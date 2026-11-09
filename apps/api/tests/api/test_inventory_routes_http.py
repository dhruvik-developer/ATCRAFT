"""Inventory write routes over HTTP: lots, serial registration, transfers,
adjustment decisions, internal-issue decisions and physical counts.

The engine's rules - FEFO, the batch and serial direction gates, FIFO pricing,
the approval thresholds - are pinned at service level in
``tests/integration/test_inventory_tracking.py``, ``test_inventory_operations.py``
and ``test_inventory_fifo.py``. These drive the sixteen write routes no backend
test had reached through HTTP: the status and envelope each answers with, the
row and the audit entry it leaves behind, the 422 a payload or a document's
state earns, and the 404 another company's record answers with - never a 403,
which would confirm that it exists.

Permission gates are ``tests/unit/test_route_authorization.py``'s. The caller is
the ``api_client`` principal - every permission, not a superuser. Where
maker-checker needs two people the document is raised through its service by
``maker``, a colleague in the same company, so the principal can decide it; it
is raised under the principal's own tenant where the point is that it cannot.
Prerequisites are built through the inventory services, ``post_movement`` and
``seed_support``. Ids are held as plain values: an expected failure rolls its
savepoint back and expires whatever was loaded before it.
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
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today
from src.core.deps import Principal, TenantContext, get_current_principal
from src.core.pagination import PageParams
from src.core.permissions import Permission
from src.main import app
from src.modules.audit.models import ApprovalRequest, AuditAction, AuditLog
from src.modules.inventory.models import (
    InternalIssue,
    InventoryBalance,
    InventoryTransaction,
    PhysicalCount,
    StockAdjustment,
    StockAdjustmentLine,
    StockBatch,
    StockSerial,
    StockTransfer,
    TransactionType,
)
from src.modules.inventory.schemas import (
    InternalIssueCreate,
    PhysicalCountCreate,
    PhysicalCountEntry,
    PhysicalCountEntryRequest,
    StockAdjustmentCreate,
    StockBatchAction,
    StockBatchCreate,
    StockSerialBulkCreate,
    StockTransferCreate,
)
from src.modules.inventory.service import (
    InternalIssueService,
    PhysicalCountService,
    StockAdjustmentService,
    StockTransferService,
    TraceabilityService,
    post_movement,
)
from src.workers import celery_app as celery_module
from tests import seed_support
from tests.integration.test_inventory_operations import _bin, _department

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Checked at the goods-in desk"
#: Ids no row carries. Body validation answers before any lookup is made.
NIL = "019a0000-0000-7000-8000-000000000001"
NIL_2 = "019a0000-0000-7000-8000-000000000002"


# -----------------------------------------------------------------------------
# Callers, envelopes and readers
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[str]:
    """Approval decisions fan out notifications; nothing reaches a broker."""
    sent: list[str] = []

    def accepted(name: str, **_: Any) -> Any:
        sent.append(name)
        return SimpleNamespace(id=uuid.uuid4().hex)

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


@pytest.fixture
async def foreign(session: AsyncSession, other_company) -> TenantContext:
    """Scope for building another company's rows, through its own actor."""
    owner = await seed_support.seed_user(session, company_id=other_company.id)
    return TenantContext(company_id=other_company.id, user_id=owner.id)


@pytest.fixture
async def maker(session: AsyncSession, tenant: TenantContext) -> TenantContext:
    """A colleague who raises the documents the principal is asked to decide."""
    colleague = await seed_support.seed_user(
        session, company_id=tenant.company_id, full_name="Store keeper"
    )
    return TenantContext(company_id=tenant.company_id, user_id=colleague.id)


def act_as(principal: Principal) -> None:
    app.dependency_overrides[get_current_principal] = lambda: principal


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
    session: AsyncSession, company_id: uuid.UUID, entity_id: uuid.UUID, action: AuditAction
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
    """One stored value, read from the row rather than from the identity map."""
    model = attribute.class_
    return await session.scalar(select(attribute).where(model.id == row_id))


async def rows_where(session: AsyncSession, model: Any, *criteria: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


async def position(
    session: AsyncSession,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    *,
    batch_id: uuid.UUID | None = None,
) -> Row[Any]:
    """The unbinned position's pools, straight from the balance row."""
    stmt = select(
        InventoryBalance.on_hand_quantity,
        InventoryBalance.qc_hold_quantity,
        InventoryBalance.available_quantity,
        InventoryBalance.in_transit_quantity,
    ).where(
        InventoryBalance.item_id == item_id,
        InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.location_id.is_(None),
        InventoryBalance.batch_id.is_(None)
        if batch_id is None
        else InventoryBalance.batch_id == batch_id,
    )
    return (await session.execute(stmt)).one()


async def ledger(session: AsyncSession, reference_id: uuid.UUID) -> list[Row[Any]]:
    stmt = (
        select(InventoryTransaction.transaction_type, InventoryTransaction.quantity)
        .where(InventoryTransaction.reference_id == reference_id)
        .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
    )
    return list((await session.execute(stmt)).all())


async def approval_statuses(session: AsyncSession, entity_id: uuid.UUID) -> list[str]:
    stmt = (
        select(ApprovalRequest.status)
        .where(ApprovalRequest.entity_id == entity_id)
        .order_by(ApprovalRequest.created_at, ApprovalRequest.id)
    )
    return [str(status) for status in (await session.scalars(stmt)).all()]


# -----------------------------------------------------------------------------
# Builders - every row through a service, post_movement or seed_support
# -----------------------------------------------------------------------------


async def receive(
    session: AsyncSession,
    tenant: TenantContext,
    item_id: uuid.UUID,
    warehouse_id: uuid.UUID,
    quantity: str,
    cost: str,
    *,
    batch_id: uuid.UUID | None = None,
    serial_id: uuid.UUID | None = None,
) -> None:
    await post_movement(
        session,
        tenant,
        item_id=item_id,
        warehouse_id=warehouse_id,
        quantity=Decimal(quantity),
        transaction_type=TransactionType.OPENING_BALANCE,
        reference_type="test",
        reference_id=None,
        reference_code=None,
        reason="Opening test stock",
        unit_cost=Decimal(cost),
        batch_id=batch_id,
        serial_id=serial_id,
    )


async def warehouse(
    session: AsyncSession, tenant: TenantContext, unique_code, **fields: Any
) -> uuid.UUID:
    row = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("WH"), **fields
    )
    return row.id


async def stocked(
    session: AsyncSession,
    tenant: TenantContext,
    unique_code,
    *,
    quantity: str = "20",
    cost: str = "12",
    warehouse_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """An untracked item holding ``quantity`` at ``cost`` at an unbinned position."""
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("IT"), standard_cost=Decimal(cost)
    )
    item_id = item.id
    if warehouse_id is None:
        warehouse_id = await warehouse(session, tenant, unique_code)
    await receive(session, tenant, item_id, warehouse_id, quantity, cost)
    return item_id, warehouse_id


async def lot(
    session: AsyncSession, tenant: TenantContext, unique_code, *, quantity: str = "4"
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A batch-tracked item, a warehouse and one ACTIVE lot holding ``quantity``."""
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("LOT"), is_batch_tracked=True
    )
    item_id = item.id
    warehouse_id = await warehouse(session, tenant, unique_code)
    batch = await TraceabilityService(session, tenant).create_batch(
        StockBatchCreate(item_id=item_id, batch_number=unique_code("L"))
    )
    if Decimal(quantity):
        await receive(session, tenant, item_id, warehouse_id, quantity, "10", batch_id=batch.id)
    return item_id, warehouse_id, batch.id


async def serialised(
    session: AsyncSession, tenant: TenantContext, unique_code, *, units: int = 2
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID], list[str]]:
    """A serial-tracked item with ``units`` registered units received into one warehouse."""
    item = await seed_support.seed_item(
        session, company_id=tenant.company_id, code=unique_code("SER"), is_serial_tracked=True
    )
    item_id = item.id
    warehouse_id = await warehouse(session, tenant, unique_code)
    registered = await TraceabilityService(session, tenant).bulk_create_serials(
        StockSerialBulkCreate(
            item_id=item_id,
            serial_numbers=[unique_code("SN") for _ in range(units)],
            warehouse_id=warehouse_id,
            unit_cost=Decimal("31"),
        )
    )
    for unit in registered:
        await receive(session, tenant, item_id, warehouse_id, "1", "31", serial_id=unit.id)
    return (
        item_id,
        warehouse_id,
        [unit.id for unit in registered],
        [u.serial_number for u in registered],
    )


def transfer_body(
    source: uuid.UUID | str,
    destination: uuid.UUID | str,
    *,
    item: uuid.UUID | str = NIL,
    **line: Any,
) -> dict[str, Any]:
    return {
        "from_warehouse_id": str(source),
        "to_warehouse_id": str(destination),
        "reason": "Feed the assembly store",
        "lines": [{"item_id": str(item), "quantity": "5", **line}],
    }


async def draft_transfer(session: AsyncSession, tenant: TenantContext, unique_code) -> uuid.UUID:
    item_id, source_id = await stocked(session, tenant, unique_code)
    destination_id = await warehouse(session, tenant, unique_code)
    transfer = await StockTransferService(session, tenant).create(
        StockTransferCreate.model_validate(transfer_body(source_id, destination_id, item=item_id))
    )
    return transfer.id


def adjustment(
    warehouse_id: uuid.UUID | str,
    item_id: uuid.UUID | str,
    quantity: str,
    *,
    reason_code: str = "CORRECTION",
    reason: str = "Recount of the rack",
    **line: Any,
) -> dict[str, Any]:
    return {
        "warehouse_id": str(warehouse_id),
        "reason_code": reason_code,
        "reason": reason,
        "lines": [{"item_id": str(item_id), "quantity": quantity, **line}],
    }


async def draft_adjustment(
    session: AsyncSession, tenant: TenantContext, unique_code
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A DRAFT writing two units up at 19: worth 38, well under any threshold."""
    item_id, warehouse_id = await stocked(session, tenant, unique_code)
    draft = await StockAdjustmentService(session, tenant).create(
        StockAdjustmentCreate.model_validate(adjustment(warehouse_id, item_id, "2", unit_cost="19"))
    )
    return draft.id, draft.lines[0].id, item_id, warehouse_id


async def pending_adjustment(
    session: AsyncSession, tenant: TenantContext, unique_code
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Six units written off at 5000 - 30000, over the default 25000 - submitted."""
    item_id, warehouse_id = await stocked(session, tenant, unique_code, cost="5000")
    service = StockAdjustmentService(session, tenant)
    draft = await service.create(
        StockAdjustmentCreate.model_validate(
            adjustment(
                warehouse_id, item_id, "-6", reason_code="DAMAGE", reason="Crushed in racking"
            )
        )
    )
    await service.submit(draft.id)
    return draft.id, item_id, warehouse_id


async def draft_issue(
    session: AsyncSession,
    tenant: TenantContext,
    unique_code,
    *,
    cost: str = "12",
    quantity: str = "1",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    item_id, warehouse_id = await stocked(session, tenant, unique_code, cost=cost)
    department = await _department(session, tenant, unique_code)
    draft = await InternalIssueService(session, tenant).create(
        InternalIssueCreate.model_validate(
            {
                "warehouse_id": str(warehouse_id),
                "reason": "Maintenance consumables",
                "department_id": str(department.id),
                "lines": [{"item_id": str(item_id), "quantity": quantity}],
            }
        )
    )
    return draft.id, item_id, warehouse_id


async def pending_issue(
    session: AsyncSession, tenant: TenantContext, unique_code
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Six units at 5000 to maintenance - over the threshold - submitted for approval."""
    issue_id, item_id, warehouse_id = await draft_issue(
        session, tenant, unique_code, cost="5000", quantity="6"
    )
    await InternalIssueService(session, tenant).submit(issue_id)
    return issue_id, item_id, warehouse_id


async def opened_count(
    session: AsyncSession, tenant: TenantContext, warehouse_id: uuid.UUID, *, start: bool = False
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    service = PhysicalCountService(session, tenant)
    count = await service.create(
        PhysicalCountCreate(warehouse_id=warehouse_id, reason="Quarterly cycle count")
    )
    lines, _ = await service.list_lines(count.id, PageParams())
    if start:
        await service.start(count.id)
    return count.id, [line.id for line in lines]


async def counted_sheet(
    session: AsyncSession, tenant: TenantContext, warehouse_id: uuid.UUID, counted: str = "18"
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    count_id, line_ids = await opened_count(session, tenant, warehouse_id, start=True)
    await PhysicalCountService(session, tenant).enter_counts(
        count_id,
        PhysicalCountEntryRequest(
            entries=[
                PhysicalCountEntry(line_id=line_id, counted_quantity=Decimal(counted))
                for line_id in line_ids
            ]
        ),
    )
    return count_id, line_ids


# -----------------------------------------------------------------------------
# Malformed bodies - refused at the boundary with the field named
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "payload", "field"),
    [
        pytest.param(
            "POST",
            "/inventory/batches",
            {"item_id": NIL, "batch_number": "   "},
            "batch_number",
            id="batch-number-blank",
        ),
        pytest.param(
            "POST",
            "/inventory/batches",
            {
                "item_id": NIL,
                "batch_number": "L-1",
                "manufactured_on": "2026-05-02",
                "expires_on": "2026-05-01",
            },
            "_root",
            id="batch-expires-before-it-was-made",
        ),
        pytest.param(
            "POST",
            "/inventory/batches",
            {"item_id": NIL, "batch_number": "L-1", "initial_quantity": "-1"},
            "initial_quantity",
            id="batch-negative-quantity",
        ),
        pytest.param(
            "POST",
            f"/inventory/batches/{NIL}/quarantine",
            {"reason": "no"},
            "reason",
            id="quarantine-reason-short",
        ),
        pytest.param(
            "POST",
            f"/inventory/batches/{NIL}/activate",
            {"reason": REASON, "status": "ACTIVE"},
            "status",
            id="activate-names-a-status",
        ),
        pytest.param(
            "POST", f"/inventory/batches/{NIL}/expire", {}, "reason", id="expire-reason-missing"
        ),
        pytest.param(
            "POST",
            "/inventory/serials/bulk",
            {"item_id": NIL, "serial_numbers": ["sn-1", " SN-1 "]},
            "serial_numbers",
            id="run-names-a-label-twice",
        ),
        pytest.param(
            "POST",
            "/inventory/serials/bulk",
            {"item_id": NIL, "serial_numbers": []},
            "serial_numbers",
            id="run-empty",
        ),
        pytest.param(
            "POST",
            "/inventory/serials",
            {"item_id": NIL, "serial_number": " \t "},
            "serial_number",
            id="serial-blank",
        ),
        pytest.param(
            "POST",
            "/inventory/serials",
            {"item_id": NIL, "serial_number": "SN-1", "unit_cost": "-1"},
            "unit_cost",
            id="serial-negative-cost",
        ),
        pytest.param(
            "POST",
            "/inventory/transfers",
            transfer_body(NIL, NIL),
            "_root",
            id="transfer-to-its-own-warehouse",
        ),
        pytest.param(
            "POST",
            "/inventory/transfers",
            transfer_body(NIL, NIL_2, quantity="0"),
            "lines.0.quantity",
            id="transfer-zero-quantity",
        ),
        pytest.param(
            "POST",
            "/inventory/transfers",
            transfer_body(NIL, NIL_2, quantity="2", serial_numbers=["SN-1"]),
            "lines.0",
            id="transfer-fewer-units-than-quantity",
        ),
        pytest.param(
            "PATCH",
            f"/inventory/transfers/{NIL}",
            {"reason": "ab"},
            "reason",
            id="transfer-edit-reason-short",
        ),
        pytest.param(
            "PATCH",
            f"/inventory/transfers/{NIL}",
            {"status": "APPROVED"},
            "status",
            id="transfer-edit-names-a-status",
        ),
        pytest.param(
            "PATCH",
            f"/inventory/adjustments/{NIL}",
            {**adjustment(NIL, NIL_2, "1"), "lines": []},
            "lines",
            id="adjustment-without-lines",
        ),
        pytest.param(
            "PATCH",
            f"/inventory/adjustments/{NIL}",
            adjustment(NIL, NIL_2, "0"),
            "lines.0.quantity",
            id="adjustment-zero-line",
        ),
        pytest.param(
            "PATCH",
            f"/inventory/adjustments/{NIL}",
            adjustment(NIL, NIL_2, "1", reason_code="LOST"),
            "reason_code",
            id="adjustment-unknown-reason-code",
        ),
        pytest.param(
            "POST",
            f"/inventory/adjustments/{NIL}/approve",
            {"note": "x" * 256},
            "note",
            id="approval-note-too-long",
        ),
        pytest.param(
            "POST",
            f"/inventory/adjustments/{NIL}/reject",
            {"note": "  a  "},
            "note",
            id="rejection-note-only-whitespace",
        ),
        pytest.param(
            "POST",
            f"/inventory/internal-issues/{NIL}/reject",
            {"reason": "no"},
            "reason",
            id="issue-rejection-reason-short",
        ),
        pytest.param(
            "POST",
            f"/inventory/internal-issues/{NIL}/cancel",
            {},
            "reason",
            id="issue-cancellation-reason-missing",
        ),
        pytest.param(
            "POST",
            "/inventory/physical-counts",
            {"warehouse_id": NIL},
            "reason",
            id="count-reason-missing",
        ),
        pytest.param(
            "POST",
            "/inventory/physical-counts",
            {"warehouse_id": "WH-MAIN", "reason": REASON},
            "warehouse_id",
            id="count-warehouse-not-an-id",
        ),
        pytest.param(
            "POST",
            f"/inventory/physical-counts/{NIL}/entries",
            {"entries": []},
            "entries",
            id="entries-empty",
        ),
        pytest.param(
            "POST",
            f"/inventory/physical-counts/{NIL}/entries",
            {"entries": [{"line_id": NIL, "counted_quantity": "1"}] * 2},
            "_root",
            id="entries-name-a-line-twice",
        ),
        pytest.param(
            "POST",
            f"/inventory/physical-counts/{NIL}/entries",
            {"entries": [{"line_id": NIL, "counted_quantity": "-1"}]},
            "entries.0.counted_quantity",
            id="entries-negative-count",
        ),
        pytest.param(
            "POST",
            f"/inventory/physical-counts/{NIL}/recount",
            {"reason": "x"},
            "reason",
            id="recount-reason-short",
        ),
    ],
)
async def test_a_malformed_body_is_refused_with_its_field_named(
    api_client: AsyncClient, method: str, path: str, payload: dict[str, Any], field: str
) -> None:
    response = await api_client.request(method, path, json=payload)
    assert field in field_errors(response)


# -----------------------------------------------------------------------------
# POST /inventory/batches
# -----------------------------------------------------------------------------


class TestCreateBatch:
    async def test_a_lot_is_recorded_normalised_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=unique_code("LOT"), is_batch_tracked=True
        )
        item_id, item_code = item.id, item.code
        number = unique_code("l").upper()

        data = ok(
            await api_client.post(
                "/inventory/batches",
                json={
                    "item_id": str(item_id),
                    "batch_number": f"  {number.lower()} ",
                    "supplier_batch_number": "V-2207",
                    "manufactured_on": (today() - timedelta(days=10)).isoformat(),
                    "expires_on": (today() + timedelta(days=170)).isoformat(),
                    "unit_cost": "14.5",
                },
            ),
            201,
        )

        assert data["batch_number"] == number
        assert (data["status"], data["item_code"]) == ("ACTIVE", item_code)
        assert data["received_on"] == today().isoformat()
        assert Decimal(data["unit_cost"]) == Decimal("14.5")
        assert Decimal(data["on_hand_quantity"]) == 0
        batch_id = uuid.UUID(data["id"])
        assert await column(session, StockBatch.supplier_batch_number, batch_id) == "V-2207"
        (created,) = await audit_rows(session, tenant.company_id, batch_id, AuditAction.CREATE)
        assert created.entity_type == "stock_batch"
        assert created.created_by_id == principal.user_id
        assert created.new_values["batch_number"] == number

    async def test_the_same_number_twice_for_one_item_is_a_duplicate(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, _, batch_id = await lot(session, tenant, unique_code, quantity="0")
        number = await column(session, StockBatch.batch_number, batch_id)

        refused(
            await api_client.post(
                "/inventory/batches",
                json={"item_id": str(item_id), "batch_number": number.lower()},
            ),
            409,
            "DUPLICATE_DOCUMENT",
        )
        assert await rows_where(session, StockBatch, StockBatch.item_id == item_id) == 1

    async def test_another_companys_item_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        theirs = await seed_support.seed_item(
            session, company_id=foreign.company_id, code=unique_code("FIT"), is_batch_tracked=True
        )
        theirs_id = theirs.id

        refused(
            await api_client.post(
                "/inventory/batches", json={"item_id": str(theirs_id), "batch_number": "L-1"}
            ),
            404,
            "NOT_FOUND",
        )
        assert await rows_where(session, StockBatch, StockBatch.item_id == theirs_id) == 0

    async def test_this_companys_vendor_is_recorded_on_the_lot(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=unique_code("LOT"), is_batch_tracked=True
        )
        item_id = item.id
        vendor_id = (await seed_support.seed_vendor(session, company_id=tenant.company_id)).id

        data = ok(
            await api_client.post(
                "/inventory/batches",
                json={"item_id": str(item_id), "batch_number": "L-1", "vendor_id": str(vendor_id)},
            ),
            201,
        )

        assert data["vendor_id"] == str(vendor_id)
        assert await column(session, StockBatch.vendor_id, uuid.UUID(data["id"])) == vendor_id

    async def test_another_companys_vendor_is_not_found(
        self, api_client, session, tenant, foreign, unique_code
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=unique_code("LOT"), is_batch_tracked=True
        )
        item_id = item.id
        vendor_id = (await seed_support.seed_vendor(session, company_id=foreign.company_id)).id

        response = await api_client.post(
            "/inventory/batches",
            json={"item_id": str(item_id), "batch_number": "L-1", "vendor_id": str(vendor_id)},
        )

        assert response.status_code == 404, f"another company's vendor accepted: {response.text}"
        assert await rows_where(session, StockBatch, StockBatch.item_id == item_id) == 0


# -----------------------------------------------------------------------------
# POST /inventory/batches/{batch_id}/quarantine | activate | expire
# -----------------------------------------------------------------------------


class TestBatchLifecycle:
    async def test_quarantine_moves_the_available_stock_to_qc_hold(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item_id, warehouse_id, batch_id = await lot(session, tenant, unique_code, quantity="4")

        data = ok(
            await api_client.post(
                f"/inventory/batches/{batch_id}/quarantine", json={"reason": REASON}
            )
        )

        assert data["status"] == "QUARANTINED"
        assert (Decimal(data["on_hand_quantity"]), Decimal(data["available_quantity"])) == (4, 0)
        held = await position(session, item_id, warehouse_id, batch_id=batch_id)
        assert (held.on_hand_quantity, held.qc_hold_quantity, held.available_quantity) == (4, 4, 0)
        [hold] = await ledger(session, batch_id)
        assert hold.transaction_type == TransactionType.QC_HOLD
        assert abs(hold.quantity) == 4
        (change,) = await audit_rows(
            session, tenant.company_id, batch_id, AuditAction.STATUS_CHANGE
        )
        assert (change.old_values, change.new_values) == (
            {"status": "ACTIVE"},
            {"status": "QUARANTINED"},
        )
        assert change.context == {"reason": REASON}
        assert change.created_by_id == principal.user_id

    async def test_activation_changes_the_status_and_leaves_the_hold(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, warehouse_id, batch_id = await lot(session, tenant, unique_code, quantity="4")
        await TraceabilityService(session, tenant).quarantine_batch(
            batch_id, StockBatchAction(reason="Damaged carton at goods-in")
        )

        data = ok(
            await api_client.post(
                f"/inventory/batches/{batch_id}/activate", json={"reason": "Sample passed"}
            )
        )

        assert data["status"] == "ACTIVE"
        # Releasing held stock is a quality decision of its own, not this button.
        assert Decimal(data["available_quantity"]) == 0
        held = await position(session, item_id, warehouse_id, batch_id=batch_id)
        assert (held.qc_hold_quantity, held.available_quantity) == (4, 0)
        changes = await audit_rows(session, tenant.company_id, batch_id, AuditAction.STATUS_CHANGE)
        assert [row.new_values["status"] for row in changes] == ["QUARANTINED", "ACTIVE"]
        assert changes[-1].context == {"reason": "Sample passed"}

    async def test_expiry_retires_the_lot_and_keeps_its_stock(
        self, api_client, session, tenant, unique_code
    ) -> None:
        _, _, batch_id = await lot(session, tenant, unique_code, quantity="4")

        data = ok(
            await api_client.post(
                f"/inventory/batches/{batch_id}/expire", json={"reason": "Shelf life is over"}
            )
        )

        assert data["status"] == "EXPIRED"
        assert Decimal(data["on_hand_quantity"]) == 4
        assert await column(session, StockBatch.status, batch_id) == "EXPIRED"
        (change,) = await audit_rows(
            session, tenant.company_id, batch_id, AuditAction.STATUS_CHANGE
        )
        assert change.new_values == {"status": "EXPIRED"}

    @pytest.mark.parametrize(
        ("history", "verb"),
        [
            pytest.param((), "activate", id="activate-an-active-lot"),
            pytest.param(("expire",), "quarantine", id="quarantine-an-expired-lot"),
            pytest.param(("expire",), "activate", id="reactivate-an-expired-lot"),
            pytest.param(("expire",), "expire", id="expire-twice"),
        ],
    )
    async def test_an_edge_the_lifecycle_lacks_is_refused(
        self, api_client, session, tenant, unique_code, history, verb
    ) -> None:
        _, _, batch_id = await lot(session, tenant, unique_code, quantity="0")
        trace = TraceabilityService(session, tenant)
        for step in history:
            await getattr(trace, f"{step}_batch")(batch_id, StockBatchAction(reason="Earlier"))
        before = await column(session, StockBatch.status, batch_id)

        refused(
            await api_client.post(f"/inventory/batches/{batch_id}/{verb}", json={"reason": REASON}),
            422,
            "INVALID_STATE_TRANSITION",
        )
        assert await column(session, StockBatch.status, batch_id) == before

    async def test_another_companys_lot_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        _, _, theirs = await lot(session, foreign, unique_code, quantity="0")

        refused(
            await api_client.post(
                f"/inventory/batches/{theirs}/quarantine", json={"reason": REASON}
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, StockBatch.status, theirs) == "ACTIVE"


# -----------------------------------------------------------------------------
# POST /inventory/serials/bulk and POST /inventory/serials
# -----------------------------------------------------------------------------


class TestRegisterSerialRun:
    async def test_a_run_is_registered_normalised_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=unique_code("SER"), is_serial_tracked=True
        )
        item_id = item.id
        store = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        store_id, store_code = store.id, store.code
        labels = [unique_code("sn").upper() for _ in range(2)]

        data = ok(
            await api_client.post(
                "/inventory/serials/bulk",
                json={
                    "item_id": str(item_id),
                    "serial_numbers": [f" {labels[0].lower()}\n", labels[1].lower()],
                    "warehouse_id": str(store_id),
                    "unit_cost": "31",
                },
            ),
            201,
        )

        assert [row["serial_number"] for row in data] == labels
        assert {(row["status"], row["warehouse_code"]) for row in data} == {
            ("IN_STOCK", store_code)
        }
        stored = await session.scalars(
            select(StockSerial.serial_number).where(StockSerial.item_id == item_id)
        )
        assert sorted(stored.all()) == sorted(labels)
        created = await session.execute(
            select(AuditLog.entity_id, AuditLog.entity_type, AuditLog.created_by_id).where(
                AuditLog.company_id == tenant.company_id,
                AuditLog.action == AuditAction.CREATE,
                AuditLog.entity_id.in_([uuid.UUID(row["id"]) for row in data]),
            )
        )
        assert sorted(
            (str(row.entity_id), row.entity_type, row.created_by_id) for row in created
        ) == (sorted((row["id"], "stock_serial", principal.user_id) for row in data))

    async def test_a_label_already_registered_is_a_duplicate(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, _, _, [existing] = await serialised(session, tenant, unique_code, units=1)

        error = refused(
            await api_client.post(
                "/inventory/serials/bulk",
                json={"item_id": str(item_id), "serial_numbers": [unique_code("SN"), existing]},
            ),
            409,
            "DUPLICATE_DOCUMENT",
        )
        assert error["details"]["duplicates"] == [existing]
        assert await rows_where(session, StockSerial, StockSerial.item_id == item_id) == 1

    @pytest.mark.parametrize(
        ("case", "status", "code"),
        [
            pytest.param("untracked", 422, "BUSINESS_RULE_VIOLATION", id="item-not-serialised"),
            pytest.param("another-items-lot", 422, "BUSINESS_RULE_VIOLATION", id="other-items-lot"),
            pytest.param("bin-elsewhere", 422, "BUSINESS_RULE_VIOLATION", id="bin-in-other-store"),
            pytest.param("foreign-store", 404, "NOT_FOUND", id="another-companys-store"),
        ],
    )
    async def test_a_run_naming_the_wrong_thing_is_refused(
        self, api_client, session, tenant, foreign, unique_code, case, status, code
    ) -> None:
        item = await seed_support.seed_item(
            session,
            company_id=tenant.company_id,
            code=unique_code("SER"),
            is_serial_tracked=case != "untracked",
        )
        item_id = item.id
        payload: dict[str, Any] = {
            "item_id": str(item_id),
            "serial_numbers": [unique_code("SN")],
            "warehouse_id": str(await warehouse(session, tenant, unique_code)),
        }
        if case == "another-items-lot":
            payload["batch_id"] = str((await lot(session, tenant, unique_code, quantity="0"))[2])
        elif case == "bin-elsewhere":
            elsewhere = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=unique_code("WH")
            )
            payload["location_id"] = str(
                (await _bin(session, tenant.company_id, elsewhere, unique_code)).id
            )
        elif case == "foreign-store":
            payload["warehouse_id"] = str(await warehouse(session, foreign, unique_code))

        refused(await api_client.post("/inventory/serials/bulk", json=payload), status, code)
        assert await rows_where(session, StockSerial, StockSerial.item_id == item_id) == 0


class TestRecordSerial:
    async def test_one_unit_is_recorded_at_its_bin_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=unique_code("SER"), is_serial_tracked=True
        )
        item_id = item.id
        store = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        store_id, store_code = store.id, store.code
        rack_id = (await _bin(session, tenant.company_id, store, unique_code)).id
        label = unique_code("sn").upper()

        data = ok(
            await api_client.post(
                "/inventory/serials",
                json={
                    "item_id": str(item_id),
                    "serial_number": f" {label[:3].lower()} {label[3:]} ",
                    "warehouse_id": str(store_id),
                    "location_id": str(rack_id),
                    "unit_cost": "42",
                    "notes": "Pump body",
                },
            ),
            201,
        )

        assert data["serial_number"] == label
        assert (data["status"], data["warehouse_code"], data["location_id"]) == (
            "IN_STOCK",
            store_code,
            str(rack_id),
        )
        assert data["received_on"] == today().isoformat()
        serial_id = uuid.UUID(data["id"])
        assert await column(session, StockSerial.serial_number, serial_id) == label
        (created,) = await audit_rows(session, tenant.company_id, serial_id, AuditAction.CREATE)
        assert (created.entity_type, created.created_by_id) == ("stock_serial", principal.user_id)
        assert created.new_values["serial_number"] == label

    async def test_the_same_label_twice_is_a_duplicate(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, _, _, [existing] = await serialised(session, tenant, unique_code, units=1)

        refused(
            await api_client.post(
                "/inventory/serials",
                json={"item_id": str(item_id), "serial_number": existing.lower()},
            ),
            409,
            "DUPLICATE_DOCUMENT",
        )
        assert await rows_where(session, StockSerial, StockSerial.item_id == item_id) == 1

    async def test_an_untracked_item_has_no_serials_to_record(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, _ = await stocked(session, tenant, unique_code)

        refused(
            await api_client.post(
                "/inventory/serials", json={"item_id": str(item_id), "serial_number": "SN-1"}
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await rows_where(session, StockSerial, StockSerial.item_id == item_id) == 0

    async def test_another_companys_warehouse_is_not_found(
        self, api_client, session, tenant, foreign, unique_code
    ) -> None:
        item = await seed_support.seed_item(
            session, company_id=tenant.company_id, code=unique_code("SER"), is_serial_tracked=True
        )
        item_id = item.id
        theirs = await warehouse(session, foreign, unique_code)

        refused(
            await api_client.post(
                "/inventory/serials",
                json={
                    "item_id": str(item_id),
                    "serial_number": "SN-1",
                    "warehouse_id": str(theirs),
                },
            ),
            404,
            "NOT_FOUND",
        )
        assert await rows_where(session, StockSerial, StockSerial.item_id == item_id) == 0


# -----------------------------------------------------------------------------
# POST /inventory/transfers
# -----------------------------------------------------------------------------


class TestCreateTransfer:
    async def test_a_draft_is_raised_with_its_lines_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item_id, source_id = await stocked(session, tenant, unique_code)
        destination = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        destination_id, destination_code = destination.id, destination.code
        expected = (today() + timedelta(days=3)).isoformat()

        data = ok(
            await api_client.post(
                "/inventory/transfers",
                json={
                    **transfer_body(source_id, destination_id, item=item_id),
                    "expected_date": expected,
                },
            ),
            201,
        )

        assert data["code"] and data["status"] == "DRAFT"
        assert (data["from_warehouse_id"], data["to_warehouse_code"]) == (
            str(source_id),
            destination_code,
        )
        assert (data["line_count"], Decimal(data["total_quantity"])) == (1, 5)
        assert data["expected_date"] == expected
        [line] = data["lines"]
        assert (
            line["item_id"],
            Decimal(line["quantity"]),
            Decimal(line["dispatched_quantity"]),
        ) == (
            str(item_id),
            5,
            0,
        )
        transfer_id = uuid.UUID(data["id"])
        assert await column(session, StockTransfer.status, transfer_id) == "DRAFT"
        # Raising the document moves nothing; dispatching it will.
        assert (await position(session, item_id, source_id)).on_hand_quantity == 20
        (created,) = await audit_rows(session, tenant.company_id, transfer_id, AuditAction.CREATE)
        assert (created.entity_type, created.created_by_id) == ("stock_transfer", principal.user_id)
        assert created.new_values["code"] == data["code"]

    async def test_scanned_labels_become_the_units_on_the_line(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, source_id, serial_ids, labels = await serialised(session, tenant, unique_code)
        destination_id = await warehouse(session, tenant, unique_code)

        data = ok(
            await api_client.post(
                "/inventory/transfers",
                json=transfer_body(
                    source_id,
                    destination_id,
                    item=item_id,
                    quantity="2",
                    serial_numbers=[label.lower() for label in labels],
                ),
            ),
            201,
        )

        assert data["lines"][0]["serial_ids"] == [str(value) for value in serial_ids]

    @pytest.mark.parametrize(
        ("case", "code"),
        [
            pytest.param("lot-unnamed", "BATCH_REQUIRED", id="batch-tracked-without-its-lot"),
            pytest.param("units-unnamed", "SERIAL_REQUIRED", id="serialised-without-units"),
            pytest.param("label-unknown", "SERIAL_REQUIRED", id="unregistered-label"),
        ],
    )
    async def test_a_line_that_could_never_dispatch_is_refused_when_keyed(
        self, api_client, session, tenant, unique_code, case, code
    ) -> None:
        if case == "lot-unnamed":
            item_id, source_id, _ = await lot(session, tenant, unique_code)
            line: dict[str, Any] = {"quantity": "2"}
        else:
            item_id, source_id, _, _ = await serialised(session, tenant, unique_code)
            line = {"quantity": "1"}
            if case == "label-unknown":
                line["serial_numbers"] = [unique_code("SN")]
        destination_id = await warehouse(session, tenant, unique_code)

        refused(
            await api_client.post(
                "/inventory/transfers",
                json=transfer_body(source_id, destination_id, item=item_id, **line),
            ),
            422,
            code,
        )
        assert (
            await rows_where(session, StockTransfer, StockTransfer.from_warehouse_id == source_id)
            == 0
        )

    @pytest.mark.parametrize(
        ("whose", "status", "code"),
        [
            pytest.param("foreign", 404, "NOT_FOUND", id="another-companys-destination"),
            pytest.param("inactive", 422, "BUSINESS_RULE_VIOLATION", id="inactive-destination"),
        ],
    )
    async def test_a_destination_that_cannot_take_part_is_refused(
        self, api_client, session, tenant, foreign, unique_code, whose, status, code
    ) -> None:
        item_id, source_id = await stocked(session, tenant, unique_code)
        if whose == "foreign":
            destination_id = await warehouse(session, foreign, unique_code)
        else:
            destination_id = await warehouse(session, tenant, unique_code, is_active=False)

        refused(
            await api_client.post(
                "/inventory/transfers", json=transfer_body(source_id, destination_id, item=item_id)
            ),
            status,
            code,
        )
        assert (
            await rows_where(session, StockTransfer, StockTransfer.from_warehouse_id == source_id)
            == 0
        )

    async def test_a_line_naming_its_own_lot_keeps_it(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, source_id, batch_id = await lot(session, tenant, unique_code)
        destination_id = await warehouse(session, tenant, unique_code)

        data = ok(
            await api_client.post(
                "/inventory/transfers",
                json=transfer_body(
                    source_id, destination_id, item=item_id, quantity="2", batch_id=str(batch_id)
                ),
            ),
            201,
        )

        assert data["lines"][0]["batch_id"] == str(batch_id)

    async def test_a_line_naming_another_companys_lot_is_not_found(
        self, api_client, session, tenant, foreign, unique_code
    ) -> None:
        item_id, source_id, _ = await lot(session, tenant, unique_code)
        _, _, theirs = await lot(session, foreign, unique_code, quantity="0")
        destination_id = await warehouse(session, tenant, unique_code)

        response = await api_client.post(
            "/inventory/transfers",
            json=transfer_body(
                source_id, destination_id, item=item_id, quantity="2", batch_id=str(theirs)
            ),
        )

        assert response.status_code == 404, f"another company's lot accepted: {response.text}"
        assert (
            await rows_where(session, StockTransfer, StockTransfer.from_warehouse_id == source_id)
            == 0
        )

    async def test_a_line_naming_another_items_lot_is_refused_on_that_line(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, source_id, _ = await lot(session, tenant, unique_code)
        _, _, other_items_lot = await lot(session, tenant, unique_code, quantity="0")
        destination_id = await warehouse(session, tenant, unique_code)

        error = refused(
            await api_client.post(
                "/inventory/transfers",
                json=transfer_body(
                    source_id,
                    destination_id,
                    item=item_id,
                    quantity="2",
                    batch_id=str(other_items_lot),
                ),
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )

        assert error["details"] == {"line_number": 1, "batch_id": str(other_items_lot)}
        assert (
            await rows_where(session, StockTransfer, StockTransfer.from_warehouse_id == source_id)
            == 0
        )


# -----------------------------------------------------------------------------
# PATCH /inventory/transfers/{transfer_id}
# -----------------------------------------------------------------------------


class TestUpdateTransfer:
    async def test_the_draft_header_is_edited_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        transfer_id = await draft_transfer(session, tenant, unique_code)
        expected = (today() + timedelta(days=9)).isoformat()

        data = ok(
            await api_client.patch(
                f"/inventory/transfers/{transfer_id}",
                json={
                    "reason": "Feed the paint shop instead",
                    "expected_date": expected,
                    "notes": "Second lorry",
                },
            )
        )

        assert (data["reason"], data["expected_date"], data["notes"]) == (
            "Feed the paint shop instead",
            expected,
            "Second lorry",
        )
        assert data["status"] == "DRAFT"
        assert await column(session, StockTransfer.reason, transfer_id) == (
            "Feed the paint shop instead"
        )
        (update,) = await audit_rows(session, tenant.company_id, transfer_id, AuditAction.UPDATE)
        assert (update.entity_type, update.created_by_id) == ("stock_transfer", principal.user_id)
        assert update.changed_fields == ["expected_date", "notes", "reason"]
        assert update.old_values["reason"] == "Feed the assembly store"
        assert update.new_values["reason"] == "Feed the paint shop instead"

    async def test_an_edit_that_names_nothing_is_refused(
        self, api_client, session, tenant, unique_code
    ) -> None:
        transfer_id = await draft_transfer(session, tenant, unique_code)

        refused(
            await api_client.patch(f"/inventory/transfers/{transfer_id}", json={}),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await audit_rows(session, tenant.company_id, transfer_id, AuditAction.UPDATE) == []

    async def test_an_approved_transfer_cannot_be_edited(
        self, api_client, session, tenant, unique_code
    ) -> None:
        transfer_id = await draft_transfer(session, tenant, unique_code)
        await StockTransferService(session, tenant).approve(transfer_id)

        refused(
            await api_client.patch(
                f"/inventory/transfers/{transfer_id}", json={"reason": "Too late to change"}
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, StockTransfer.reason, transfer_id) == "Feed the assembly store"

    async def test_another_companys_transfer_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        theirs = await draft_transfer(session, foreign, unique_code)

        refused(
            await api_client.patch(f"/inventory/transfers/{theirs}", json={"notes": "Probe"}),
            404,
            "NOT_FOUND",
        )
        assert await column(session, StockTransfer.notes, theirs) is None

    async def test_a_null_reason_is_refused_as_a_field_error(
        self, api_client, session, tenant, unique_code
    ) -> None:
        transfer_id = await draft_transfer(session, tenant, unique_code)

        response = await api_client.patch(
            f"/inventory/transfers/{transfer_id}", json={"reason": None}
        )

        assert response.status_code == 422, response.text
        assert "reason" in (response.json()["error"].get("details") or {}).get("fields", {}), (
            f"no field error for the null reason: {response.text}"
        )
        assert await column(session, StockTransfer.reason, transfer_id) == "Feed the assembly store"

    async def test_a_null_clears_the_optional_header_fields(
        self, api_client, session, tenant, unique_code
    ) -> None:
        transfer_id = await draft_transfer(session, tenant, unique_code)
        ok(
            await api_client.patch(
                f"/inventory/transfers/{transfer_id}",
                json={"expected_date": today().isoformat(), "notes": "Second lorry"},
            )
        )

        data = ok(
            await api_client.patch(
                f"/inventory/transfers/{transfer_id}",
                json={"expected_date": None, "notes": None},
            )
        )

        assert (data["expected_date"], data["notes"]) == (None, None)
        assert await column(session, StockTransfer.notes, transfer_id) is None
        assert await column(session, StockTransfer.reason, transfer_id) == "Feed the assembly store"


# -----------------------------------------------------------------------------
# PATCH /inventory/adjustments/{adjustment_id}
# -----------------------------------------------------------------------------


class TestUpdateAdjustment:
    async def test_a_draft_is_replaced_repriced_and_audited(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        draft_id, old_line_id, item_id, warehouse_id = await draft_adjustment(
            session, tenant, unique_code
        )

        data = ok(
            await api_client.patch(
                f"/inventory/adjustments/{draft_id}",
                json=adjustment(
                    warehouse_id, item_id, "3", unit_cost="19", reason="Recounted the top rack"
                ),
            )
        )

        assert (data["status"], data["reason"]) == ("DRAFT", "Recounted the top rack")
        assert Decimal(data["total_value"]) == 57
        [line] = data["lines"]
        assert line["id"] != str(old_line_id)
        assert (
            Decimal(line["quantity"]),
            Decimal(line["unit_cost"]),
            Decimal(line["total_cost"]),
        ) == (
            3,
            19,
            57,
        )
        assert await column(session, StockAdjustment.total_value, draft_id) == 57
        (update,) = await audit_rows(session, tenant.company_id, draft_id, AuditAction.UPDATE)
        assert (update.entity_type, update.created_by_id) == ("stock_adjustment", principal.user_id)
        assert {"reason", "total_value"} <= set(update.changed_fields)
        assert (
            Decimal(update.old_values["total_value"]),
            Decimal(update.new_values["total_value"]),
        ) == (38, 57)
        # A draft moves no stock, however often it is edited.
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 20

    async def test_only_a_draft_can_be_edited(
        self, api_client, session, tenant, unique_code
    ) -> None:
        adjustment_id, item_id, warehouse_id = await pending_adjustment(
            session, tenant, unique_code
        )

        refused(
            await api_client.patch(
                f"/inventory/adjustments/{adjustment_id}",
                json=adjustment(warehouse_id, item_id, "-1"),
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, StockAdjustment.status, adjustment_id) == "PENDING_APPROVAL"

    async def test_stating_a_cost_needs_a_cost_right(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        draft_id, _, item_id, warehouse_id = await draft_adjustment(session, tenant, unique_code)
        hidden = {str(Permission.ITEM_COST_VIEW), str(Permission.INVENTORY_VALUATION_VIEW)}
        act_as(dataclasses.replace(principal, permissions=principal.permissions - hidden))

        refused(
            await api_client.patch(
                f"/inventory/adjustments/{draft_id}",
                json=adjustment(warehouse_id, item_id, "3", unit_cost="1"),
            ),
            403,
            "PERMISSION_DENIED",
        )
        assert await column(session, StockAdjustment.total_value, draft_id) == 38

    async def test_another_companys_adjustment_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        theirs, _, item_id, warehouse_id = await draft_adjustment(session, foreign, unique_code)

        refused(
            await api_client.patch(
                f"/inventory/adjustments/{theirs}", json=adjustment(warehouse_id, item_id, "3")
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, StockAdjustment.total_value, theirs) == 38

    async def test_moving_a_draft_into_another_companys_warehouse_is_not_found(
        self, api_client, session, tenant, foreign, unique_code
    ) -> None:
        draft_id, _, item_id, warehouse_id = await draft_adjustment(session, tenant, unique_code)
        theirs = await warehouse(session, foreign, unique_code)

        refused(
            await api_client.patch(
                f"/inventory/adjustments/{draft_id}", json=adjustment(theirs, item_id, "3")
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, StockAdjustment.warehouse_id, draft_id) == warehouse_id

    async def test_a_line_naming_another_companys_lot_is_not_found(
        self, api_client, session, tenant, foreign, unique_code
    ) -> None:
        item_id, warehouse_id, mine = await lot(session, tenant, unique_code)
        draft = await StockAdjustmentService(session, tenant).create(
            StockAdjustmentCreate.model_validate(
                adjustment(warehouse_id, item_id, "2", unit_cost="19", batch_id=str(mine))
            )
        )
        draft_id = draft.id
        _, _, theirs = await lot(session, foreign, unique_code, quantity="0")

        response = await api_client.patch(
            f"/inventory/adjustments/{draft_id}",
            json=adjustment(warehouse_id, item_id, "2", unit_cost="19", batch_id=str(theirs)),
        )

        assert response.status_code == 404, f"another company's lot accepted: {response.text}"
        stored = await session.scalars(
            select(StockAdjustmentLine.batch_id).where(
                StockAdjustmentLine.adjustment_id == draft_id
            )
        )
        assert list(stored) == [mine]

    @pytest.mark.parametrize(
        ("whose", "status", "code"),
        [
            pytest.param("foreign", 404, "NOT_FOUND", id="another-companys-lot"),
            pytest.param("other-item", 422, "BUSINESS_RULE_VIOLATION", id="another-items-lot"),
        ],
    )
    async def test_a_new_draft_naming_a_lot_it_cannot_hold_is_refused(
        self, api_client, session, tenant, foreign, unique_code, whose, status, code
    ) -> None:
        item_id, warehouse_id, _ = await lot(session, tenant, unique_code)
        owner = foreign if whose == "foreign" else tenant
        _, _, wrong_lot = await lot(session, owner, unique_code, quantity="0")

        refused(
            await api_client.post(
                "/inventory/adjustments",
                json=adjustment(
                    warehouse_id, item_id, "2", unit_cost="19", batch_id=str(wrong_lot)
                ),
            ),
            status,
            code,
        )
        assert (
            await rows_where(session, StockAdjustment, StockAdjustment.warehouse_id == warehouse_id)
            == 0
        )

    @pytest.mark.parametrize(
        ("whose", "status", "code"),
        [
            pytest.param("foreign", 404, "NOT_FOUND", id="another-companys-bin"),
            pytest.param("elsewhere", 422, "BUSINESS_RULE_VIOLATION", id="another-warehouses-bin"),
            pytest.param("inactive", 422, "BUSINESS_RULE_VIOLATION", id="write-up-into-a-shut-bin"),
        ],
    )
    async def test_a_new_draft_naming_a_bin_it_cannot_use_is_refused(
        self, api_client, session, tenant, foreign, unique_code, whose, status, code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        if whose == "foreign":
            store = await seed_support.seed_warehouse(
                session, company_id=foreign.company_id, code=unique_code("WH")
            )
            bin_id = (await _bin(session, foreign.company_id, store, unique_code)).id
        elif whose == "elsewhere":
            store = await seed_support.seed_warehouse(
                session, company_id=tenant.company_id, code=unique_code("WH")
            )
            bin_id = (await _bin(session, tenant.company_id, store, unique_code)).id
        else:
            here = SimpleNamespace(id=warehouse_id)
            bin_id = (await _bin(session, tenant.company_id, here, unique_code, is_active=False)).id

        error = refused(
            await api_client.post(
                "/inventory/adjustments",
                json=adjustment(
                    warehouse_id, item_id, "2", unit_cost="19", location_id=str(bin_id)
                ),
            ),
            status,
            code,
        )

        if status == 422:
            assert error["details"]["line_number"] == 1
            assert error["details"]["location_id"] == str(bin_id)
        assert (
            await rows_where(session, StockAdjustment, StockAdjustment.warehouse_id == warehouse_id)
            == 0
        )

    async def test_a_draft_keeps_the_bins_of_its_own_warehouse(
        self, api_client, session, tenant, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        here = SimpleNamespace(id=warehouse_id)
        rack_id = (await _bin(session, tenant.company_id, here, unique_code)).id
        # A shut bin still gives its stock up: a write-down out of it is a draft.
        shut = await _bin(session, tenant.company_id, here, unique_code)
        shut_id = shut.id
        await post_movement(
            session,
            tenant,
            item_id=item_id,
            warehouse_id=warehouse_id,
            location_id=shut_id,
            quantity=Decimal("3"),
            transaction_type=TransactionType.OPENING_BALANCE,
            reference_type="test",
            reference_id=None,
            reference_code=None,
            reason="Opening test stock",
            unit_cost=Decimal("12"),
        )
        shut.is_active = False
        await session.flush()
        body = adjustment(warehouse_id, item_id, "2", unit_cost="19", location_id=str(rack_id))
        body["lines"].append(
            {"item_id": str(item_id), "quantity": "-1", "location_id": str(shut_id)}
        )

        data = ok(await api_client.post("/inventory/adjustments", json=body), 201)

        assert [line["location_id"] for line in data["lines"]] == [str(rack_id), str(shut_id)]
        stored = await session.scalars(
            select(StockAdjustmentLine.location_id)
            .where(StockAdjustmentLine.adjustment_id == uuid.UUID(data["id"]))
            .order_by(StockAdjustmentLine.line_number)
        )
        assert list(stored) == [rack_id, shut_id]

    async def test_a_line_moved_to_another_companys_bin_is_not_found(
        self, api_client, session, tenant, foreign, unique_code
    ) -> None:
        item_id, warehouse_id = await stocked(session, tenant, unique_code)
        rack_id = (
            await _bin(session, tenant.company_id, SimpleNamespace(id=warehouse_id), unique_code)
        ).id
        draft = await StockAdjustmentService(session, tenant).create(
            StockAdjustmentCreate.model_validate(
                adjustment(warehouse_id, item_id, "2", unit_cost="19", location_id=str(rack_id))
            )
        )
        draft_id = draft.id
        store = await seed_support.seed_warehouse(
            session, company_id=foreign.company_id, code=unique_code("WH")
        )
        theirs = (await _bin(session, foreign.company_id, store, unique_code)).id

        response = await api_client.patch(
            f"/inventory/adjustments/{draft_id}",
            json=adjustment(warehouse_id, item_id, "2", unit_cost="19", location_id=str(theirs)),
        )

        refused(response, 404, "NOT_FOUND")
        stored = await session.scalars(
            select(StockAdjustmentLine.location_id).where(
                StockAdjustmentLine.adjustment_id == draft_id
            )
        )
        assert list(stored) == [rack_id]


# -----------------------------------------------------------------------------
# POST /inventory/adjustments/{adjustment_id}/approve | reject
# -----------------------------------------------------------------------------


class TestDecideAdjustment:
    async def test_a_second_person_approves_and_nothing_moves_yet(
        self, api_client, session, tenant, principal, maker, unique_code
    ) -> None:
        adjustment_id, item_id, warehouse_id = await pending_adjustment(session, maker, unique_code)

        data = ok(
            await api_client.post(
                f"/inventory/adjustments/{adjustment_id}/approve",
                json={"note": "  Photographed   the damage "},
            )
        )

        assert data["status"] == "APPROVED"
        assert (data["approved_by_id"], data["decision_note"]) == (
            str(principal.user_id),
            "Photographed the damage",
        )
        assert data["approved_at"] is not None and data["posted_at"] is None
        assert await column(session, StockAdjustment.status, adjustment_id) == "APPROVED"
        (approval,) = await audit_rows(
            session, tenant.company_id, adjustment_id, AuditAction.APPROVE
        )
        assert (approval.old_values, approval.new_values) == (
            {"status": "PENDING_APPROVAL"},
            {"status": "APPROVED"},
        )
        assert approval.context == {"reason": "Photographed the damage"}
        assert approval.created_by_id == principal.user_id
        assert await approval_statuses(session, adjustment_id) == ["APPROVED"]
        # Approving is a signature, not a posting.
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 20

    async def test_a_second_person_rejects_with_a_reason(
        self, api_client, session, tenant, principal, maker, unique_code
    ) -> None:
        adjustment_id, item_id, warehouse_id = await pending_adjustment(session, maker, unique_code)

        data = ok(
            await api_client.post(
                f"/inventory/adjustments/{adjustment_id}/reject",
                json={"note": "Photograph the damage first"},
            )
        )

        assert (data["status"], data["decision_note"]) == (
            "REJECTED",
            "Photograph the damage first",
        )
        assert await column(session, StockAdjustment.status, adjustment_id) == "REJECTED"
        (rejection,) = await audit_rows(
            session, tenant.company_id, adjustment_id, AuditAction.REJECT
        )
        assert rejection.new_values == {"status": "REJECTED"}
        assert rejection.context == {"reason": "Photograph the damage first"}
        assert rejection.created_by_id == principal.user_id
        assert await approval_statuses(session, adjustment_id) == ["REJECTED"]
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 20

    @pytest.mark.parametrize(
        ("verb", "payload"),
        [
            pytest.param("approve", {}, id="approve"),
            pytest.param("reject", {"note": "Not convinced"}, id="reject"),
        ],
    )
    async def test_the_raiser_cannot_decide_their_own_adjustment(
        self, api_client, session, tenant, unique_code, verb, payload
    ) -> None:
        adjustment_id, _, _ = await pending_adjustment(session, tenant, unique_code)

        refused(
            await api_client.post(f"/inventory/adjustments/{adjustment_id}/{verb}", json=payload),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, StockAdjustment.status, adjustment_id) == "PENDING_APPROVAL"
        assert await approval_statuses(session, adjustment_id) == ["PENDING"]

    @pytest.mark.parametrize(
        ("verb", "payload"),
        [
            pytest.param("approve", {}, id="approve-a-draft"),
            pytest.param("reject", {"note": "Not convinced"}, id="reject-a-draft"),
        ],
    )
    async def test_a_draft_is_not_waiting_for_a_decision(
        self, api_client, session, maker, unique_code, verb, payload
    ) -> None:
        draft_id, _, _, _ = await draft_adjustment(session, maker, unique_code)

        refused(
            await api_client.post(f"/inventory/adjustments/{draft_id}/{verb}", json=payload),
            422,
            "INVALID_STATE_TRANSITION",
        )
        assert await column(session, StockAdjustment.status, draft_id) == "DRAFT"


class TestCancelAdjustment:
    async def test_cancelling_a_pending_adjustment_withdraws_it_from_the_approval_queue(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        adjustment_id, item_id, warehouse_id = await pending_adjustment(
            session, tenant, unique_code
        )

        data = ok(
            await api_client.post(
                f"/inventory/adjustments/{adjustment_id}/cancel",
                json={"reason": "Found the cartons on another rack"},
            )
        )

        assert data["status"] == "CANCELLED"
        assert await approval_statuses(session, adjustment_id) == ["REJECTED"]
        (cancel,) = await audit_rows(session, tenant.company_id, adjustment_id, AuditAction.CANCEL)
        assert (cancel.old_values, cancel.new_values) == (
            {"status": "PENDING_APPROVAL"},
            {"status": "CANCELLED"},
        )
        assert cancel.created_by_id == principal.user_id
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 20

    async def test_a_draft_is_cancelled_with_no_queue_row_to_close(
        self, api_client, session, tenant, unique_code
    ) -> None:
        draft_id, _, _, _ = await draft_adjustment(session, tenant, unique_code)

        data = ok(
            await api_client.post(
                f"/inventory/adjustments/{draft_id}/cancel",
                json={"reason": "Keyed against the wrong item"},
            )
        )

        assert data["status"] == "CANCELLED"
        assert await approval_statuses(session, draft_id) == []


# -----------------------------------------------------------------------------
# POST /inventory/internal-issues/{issue_id}/approve | reject | cancel
# -----------------------------------------------------------------------------


class TestApproveInternalIssue:
    async def test_a_second_person_approves_it_and_the_queue_row_closes_approved(
        self, api_client, session, tenant, principal, maker, unique_code
    ) -> None:
        # The queue row's outcome is read off where the issue went, not off the
        # verb: approving is the one way from PENDING_APPROVAL to POSTED.
        issue_id, item_id, warehouse_id = await pending_issue(session, maker, unique_code)

        data = ok(await api_client.post(f"/inventory/internal-issues/{issue_id}/approve"))

        assert (data["status"], data["approved_by_id"]) == ("POSTED", str(principal.user_id))
        assert await approval_statuses(session, issue_id) == ["APPROVED"]
        (approval,) = await audit_rows(session, tenant.company_id, issue_id, AuditAction.APPROVE)
        assert (approval.old_values, approval.new_values) == (
            {"status": "PENDING_APPROVAL"},
            {"status": "POSTED"},
        )
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 14


class TestRejectInternalIssue:
    async def test_a_second_person_rejects_it_and_it_ends_cancelled(
        self, api_client, session, tenant, principal, maker, unique_code
    ) -> None:
        issue_id, item_id, warehouse_id = await pending_issue(session, maker, unique_code)

        data = ok(
            await api_client.post(
                f"/inventory/internal-issues/{issue_id}/reject",
                json={"reason": "Use the spares already on the line"},
            )
        )

        assert (data["status"], data["decision_note"]) == (
            "CANCELLED",
            "Use the spares already on the line",
        )
        assert await column(session, InternalIssue.status, issue_id) == "CANCELLED"
        (rejection,) = await audit_rows(session, tenant.company_id, issue_id, AuditAction.REJECT)
        assert (rejection.old_values, rejection.new_values) == (
            {"status": "PENDING_APPROVAL"},
            {"status": "CANCELLED"},
        )
        assert rejection.created_by_id == principal.user_id
        assert await approval_statuses(session, issue_id) == ["REJECTED"]
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 20

    async def test_the_raiser_cannot_reject_their_own_issue(
        self, api_client, session, tenant, unique_code
    ) -> None:
        issue_id, _, _ = await pending_issue(session, tenant, unique_code)

        refused(
            await api_client.post(
                f"/inventory/internal-issues/{issue_id}/reject", json={"reason": "Changed my mind"}
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, InternalIssue.status, issue_id) == "PENDING_APPROVAL"

    async def test_a_draft_is_not_waiting_for_a_decision(
        self, api_client, session, maker, unique_code
    ) -> None:
        issue_id, _, _ = await draft_issue(session, maker, unique_code)

        refused(
            await api_client.post(
                f"/inventory/internal-issues/{issue_id}/reject", json={"reason": "Not needed"}
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, InternalIssue.status, issue_id) == "DRAFT"


class TestCancelInternalIssue:
    async def test_a_draft_is_cancelled_with_its_reason(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        issue_id, item_id, warehouse_id = await draft_issue(session, tenant, unique_code)

        data = ok(
            await api_client.post(
                f"/inventory/internal-issues/{issue_id}/cancel",
                json={"reason": "Raised against the wrong store"},
            )
        )

        assert (data["status"], data["decision_note"]) == (
            "CANCELLED",
            "Raised against the wrong store",
        )
        assert data["posted_at"] is None
        assert await column(session, InternalIssue.status, issue_id) == "CANCELLED"
        (cancel,) = await audit_rows(session, tenant.company_id, issue_id, AuditAction.CANCEL)
        assert (cancel.old_values, cancel.new_values) == (
            {"status": "DRAFT"},
            {"status": "CANCELLED"},
        )
        assert cancel.context == {"reason": "Raised against the wrong store"}
        assert cancel.created_by_id == principal.user_id
        assert (await position(session, item_id, warehouse_id)).on_hand_quantity == 20

    async def test_a_posted_issue_cannot_be_cancelled(
        self, api_client, session, tenant, unique_code
    ) -> None:
        issue_id, _, _ = await draft_issue(session, tenant, unique_code)
        await InternalIssueService(session, tenant).post(issue_id)

        refused(
            await api_client.post(
                f"/inventory/internal-issues/{issue_id}/cancel", json={"reason": "Too late"}
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )
        assert await column(session, InternalIssue.status, issue_id) == "POSTED"

    async def test_another_companys_issue_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        theirs, _, _ = await draft_issue(session, foreign, unique_code)

        refused(
            await api_client.post(
                f"/inventory/internal-issues/{theirs}/cancel", json={"reason": "Scope probe"}
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, InternalIssue.status, theirs) == "DRAFT"

    async def test_cancelling_a_pending_issue_withdraws_it_from_the_approval_queue(
        self, api_client, session, tenant, unique_code
    ) -> None:
        issue_id, _, _ = await pending_issue(session, tenant, unique_code)

        data = ok(
            await api_client.post(
                f"/inventory/internal-issues/{issue_id}/cancel",
                json={"reason": "The line found spares"},
            )
        )

        assert data["status"] == "CANCELLED"
        assert await approval_statuses(session, issue_id) == ["REJECTED"], (
            "the cancelled issue is still waiting in the approval queue"
        )


# -----------------------------------------------------------------------------
# POST /inventory/physical-counts
# -----------------------------------------------------------------------------


class TestOpenCount:
    async def test_a_sheet_snapshots_every_position_in_scope(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        store = await seed_support.seed_warehouse(
            session, company_id=tenant.company_id, code=unique_code("WH")
        )
        store_id, store_code = store.id, store.code
        first, _ = await stocked(session, tenant, unique_code, warehouse_id=store_id)
        second, _ = await stocked(session, tenant, unique_code, quantity="7", warehouse_id=store_id)

        data = ok(
            await api_client.post(
                "/inventory/physical-counts",
                json={"warehouse_id": str(store_id), "reason": "Quarterly cycle count"},
            ),
            201,
        )

        assert data["code"] and data["status"] == "DRAFT"
        assert (data["warehouse_code"], data["line_count"]) == (store_code, 2)
        assert data["count_date"] == today().isoformat()
        count_id = uuid.UUID(data["id"])
        lines = ok(await api_client.get(f"/inventory/physical-counts/{count_id}/lines"))
        assert {(line["item_id"], Decimal(line["system_quantity"])) for line in lines} == {
            (str(first), 20),
            (str(second), 7),
        }
        assert {line["counted_quantity"] for line in lines} == {None}
        (created,) = await audit_rows(session, tenant.company_id, count_id, AuditAction.CREATE)
        assert (created.entity_type, created.created_by_id) == ("physical_count", principal.user_id)
        assert created.new_values["line_count"] == 2

    @pytest.mark.parametrize(
        ("case", "status", "code"),
        [
            pytest.param(
                "sheet-open", 422, "BUSINESS_RULE_VIOLATION", id="a-sheet-is-already-open"
            ),
            pytest.param("empty", 422, "BUSINESS_RULE_VIOLATION", id="nothing-to-count"),
            pytest.param("foreign", 404, "NOT_FOUND", id="another-companys-warehouse"),
        ],
    )
    async def test_a_scope_that_cannot_be_counted_is_refused(
        self, api_client, session, tenant, foreign, unique_code, case, status, code
    ) -> None:
        if case == "sheet-open":
            _, store_id = await stocked(session, tenant, unique_code)
            await opened_count(session, tenant, store_id)
        elif case == "empty":
            store_id = await warehouse(session, tenant, unique_code)
        else:
            _, store_id = await stocked(session, foreign, unique_code)
        before = await rows_where(session, PhysicalCount, PhysicalCount.warehouse_id == store_id)

        refused(
            await api_client.post(
                "/inventory/physical-counts",
                json={"warehouse_id": str(store_id), "reason": "Quarterly cycle count"},
            ),
            status,
            code,
        )
        assert (
            await rows_where(session, PhysicalCount, PhysicalCount.warehouse_id == store_id)
            == before
        )


# -----------------------------------------------------------------------------
# POST /inventory/physical-counts/{count_id}/entries
# -----------------------------------------------------------------------------


class TestEnterCounts:
    async def test_the_last_line_entered_finishes_the_sheet(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        item_id, store_id = await stocked(session, tenant, unique_code)
        count_id, [line_id] = await opened_count(session, tenant, store_id, start=True)

        data = ok(
            await api_client.post(
                f"/inventory/physical-counts/{count_id}/entries",
                json={
                    "entries": [
                        {"line_id": str(line_id), "counted_quantity": "18", "notes": "Two short"}
                    ]
                },
            )
        )

        assert data["status"] == "COUNTED" and data["counted_at"] is not None
        [line] = ok(await api_client.get(f"/inventory/physical-counts/{count_id}/lines"))
        assert (Decimal(line["counted_quantity"]), Decimal(line["variance_quantity"])) == (18, -2)
        # Two units short at the 12 the FIFO layer holds them at.
        assert Decimal(line["variance_value"]) == -24
        assert (line["counted_by_id"], line["notes"]) == (str(principal.user_id), "Two short")
        # Entering a count posts nothing; confirming the sheet does.
        assert (await position(session, item_id, store_id)).on_hand_quantity == 20
        (entered,) = await audit_rows(session, tenant.company_id, count_id, AuditAction.UPDATE)
        assert entered.context == {"entered": 1, "uncounted_remaining": 0}
        changes = await audit_rows(session, tenant.company_id, count_id, AuditAction.STATUS_CHANGE)
        assert changes[-1].new_values == {"status": "COUNTED"}

    async def test_a_partial_entry_keeps_the_sheet_in_progress(
        self, api_client, session, tenant, unique_code
    ) -> None:
        _, store_id = await stocked(session, tenant, unique_code)
        await stocked(session, tenant, unique_code, quantity="7", warehouse_id=store_id)
        count_id, line_ids = await opened_count(session, tenant, store_id, start=True)

        data = ok(
            await api_client.post(
                f"/inventory/physical-counts/{count_id}/entries",
                json={"entries": [{"line_id": str(line_ids[0]), "counted_quantity": "20"}]},
            )
        )

        assert (data["status"], data["counted_at"]) == ("IN_PROGRESS", None)
        (entered,) = await audit_rows(session, tenant.company_id, count_id, AuditAction.UPDATE)
        assert entered.context == {"entered": 1, "uncounted_remaining": 1}

    async def test_counts_need_a_sheet_in_progress(
        self, api_client, session, tenant, unique_code
    ) -> None:
        _, store_id = await stocked(session, tenant, unique_code)
        count_id, [line_id] = await opened_count(session, tenant, store_id)

        refused(
            await api_client.post(
                f"/inventory/physical-counts/{count_id}/entries",
                json={"entries": [{"line_id": str(line_id), "counted_quantity": "18"}]},
            ),
            422,
            "BUSINESS_RULE_VIOLATION",
        )
        assert await column(session, PhysicalCount.status, count_id) == "DRAFT"

    async def test_a_line_of_another_sheet_is_not_found(
        self, api_client, session, tenant, unique_code
    ) -> None:
        _, store_id = await stocked(session, tenant, unique_code)
        count_id, _ = await opened_count(session, tenant, store_id, start=True)
        _, elsewhere = await stocked(session, tenant, unique_code)
        _, [stranger] = await opened_count(session, tenant, elsewhere, start=True)

        refused(
            await api_client.post(
                f"/inventory/physical-counts/{count_id}/entries",
                json={"entries": [{"line_id": str(stranger), "counted_quantity": "1"}]},
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, PhysicalCount.status, count_id) == "IN_PROGRESS"

    async def test_another_companys_sheet_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        _, store_id = await stocked(session, foreign, unique_code)
        theirs, [line_id] = await opened_count(session, foreign, store_id, start=True)

        refused(
            await api_client.post(
                f"/inventory/physical-counts/{theirs}/entries",
                json={"entries": [{"line_id": str(line_id), "counted_quantity": "1"}]},
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, PhysicalCount.status, theirs) == "IN_PROGRESS"


# -----------------------------------------------------------------------------
# POST /inventory/physical-counts/{count_id}/recount
# -----------------------------------------------------------------------------


class TestRecount:
    async def test_a_finished_sheet_is_reopened_and_its_entries_cleared(
        self, api_client, session, tenant, principal, unique_code
    ) -> None:
        _, store_id = await stocked(session, tenant, unique_code)
        count_id, _ = await counted_sheet(session, tenant, store_id)

        data = ok(
            await api_client.post(
                f"/inventory/physical-counts/{count_id}/recount",
                json={"reason": "Supervisor disputes the top shelf"},
            )
        )

        assert (data["status"], data["counted_at"]) == ("IN_PROGRESS", None)
        [line] = ok(await api_client.get(f"/inventory/physical-counts/{count_id}/lines"))
        assert (line["counted_quantity"], line["variance_quantity"], line["counted_by_id"]) == (
            None,
            None,
            None,
        )
        changes = await audit_rows(session, tenant.company_id, count_id, AuditAction.STATUS_CHANGE)
        assert (changes[-1].old_values, changes[-1].new_values) == (
            {"status": "COUNTED"},
            {"status": "IN_PROGRESS"},
        )
        assert changes[-1].context == {"reason": "Supervisor disputes the top shelf"}
        assert changes[-1].created_by_id == principal.user_id

    async def test_a_sheet_still_being_counted_cannot_be_recounted(
        self, api_client, session, tenant, unique_code
    ) -> None:
        _, store_id = await stocked(session, tenant, unique_code)
        count_id, _ = await opened_count(session, tenant, store_id, start=True)

        refused(
            await api_client.post(
                f"/inventory/physical-counts/{count_id}/recount", json={"reason": "Start again"}
            ),
            422,
            "INVALID_STATE_TRANSITION",
        )
        assert await column(session, PhysicalCount.status, count_id) == "IN_PROGRESS"

    async def test_another_companys_sheet_is_not_found(
        self, api_client, session, foreign, unique_code
    ) -> None:
        _, store_id = await stocked(session, foreign, unique_code)
        theirs, _ = await counted_sheet(session, foreign, store_id)

        refused(
            await api_client.post(
                f"/inventory/physical-counts/{theirs}/recount", json={"reason": "Scope probe"}
            ),
            404,
            "NOT_FOUND",
        )
        assert await column(session, PhysicalCount.status, theirs) == "COUNTED"

    async def test_a_sheet_never_started_cannot_be_recounted(
        self, api_client, session, tenant, unique_code
    ) -> None:
        _, store_id = await stocked(session, tenant, unique_code)
        count_id, _ = await opened_count(session, tenant, store_id)

        response = await api_client.post(
            f"/inventory/physical-counts/{count_id}/recount", json={"reason": "Start again"}
        )

        error = refused(response, 422, "INVALID_STATE_TRANSITION")
        assert error["details"]["current_status"] == "DRAFT"
        assert await column(session, PhysicalCount.status, count_id) == "DRAFT"
        assert await column(session, PhysicalCount.started_at, count_id) is None
