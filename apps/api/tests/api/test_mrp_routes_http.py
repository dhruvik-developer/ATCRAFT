"""MRP write routes over HTTP: nightly settings, starting a run, bulk-ignoring.

The planning engine, netting and time phasing are pinned in
``tests/integration/test_mrp_service.py`` and the unit suites; the generic suites
cover every route's anonymous, unentitled and unknown-id answers. This file
walks the three handlers no test had executed through HTTP, with real rows:

* ``PUT /mrp/schedule-settings`` replaces the company's nightly configuration,
  audits the create and every update, and refuses a scope naming a store that is
  not one of this company's active warehouses.
* ``POST /mrp/runs`` answers 200 with the completed run when it is small and 202
  with a status URL when it is large - the server decides - and 409 while a run
  is genuinely in flight, closing off one that was abandoned.
* ``POST /mrp/suggestions/bulk-ignore`` dismisses the open suggestions it is
  given with one reason and one audit row each, and reports the rest as skipped.

The Redis planning lock is replaced with an in-process one (the contract, "a
second run is refused", is what matters, as in ``test_mrp_service``) and
Celery's ``send_task`` is stubbed, so nothing leaves the process.
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

from src.core.clock import today, utcnow
from src.core.config import settings
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.mrp import service as mrp_service
from src.modules.mrp.models import (
    MrpDemand,
    MrpRun,
    MrpRunMode,
    MrpRunStatus,
    MrpScheduleSettings,
    MrpSuggestion,
    MrpSuggestionStatus,
    MrpSuggestionType,
)
from src.modules.products.models import ItemGroup, ProductionType
from src.workers import celery_app as celery_module
from tests import seed_support

pytestmark = [pytest.mark.api, pytest.mark.integration]

REASON = "Covered by the blanket order"


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


def relative(status_url: str) -> str:
    """A status URL is absolute under the API prefix; the client already carries it."""
    prefix = settings.api_v1_prefix.rstrip("/")
    assert status_url.startswith(prefix), status_url
    return status_url[len(prefix) :]


async def audit_trail(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID | None = None
) -> list[AuditLog]:
    # (created_at, id): every row written in one transaction shares now().
    stmt = select(AuditLog).where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return list((await session.scalars(stmt.order_by(AuditLog.created_at, AuditLog.id))).all())


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def planning_lock(monkeypatch) -> dict[str, str]:
    """The Redis planning lock, in process."""
    held: dict[str, str] = {}

    async def acquire(name: str, ttl_seconds: int = 300, token: str | None = None) -> str | None:
        if name in held:
            return None
        held[name] = token or uuid.uuid4().hex
        return held[name]

    async def release(name: str, token: str) -> bool:
        if held.get(name) == token:
            del held[name]
            return True
        return False

    monkeypatch.setattr(mrp_service, "acquire_lock", acquire)
    monkeypatch.setattr(mrp_service, "release_lock", release)
    return held


@pytest.fixture(autouse=True)
def broker(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """Every task handed to Celery, with its kwargs. Nothing reaches a broker."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def accepted(name: str, **options: Any) -> Any:
        sent.append((name, options.get("kwargs") or {}))
        return SimpleNamespace(id=f"task-{uuid.uuid4().hex[:8]}")

    monkeypatch.setattr(celery_module.celery_app, "send_task", accepted)
    return sent


async def buy_item(session: AsyncSession, company_id: uuid.UUID, code: str) -> uuid.UUID:
    item = await seed_support.seed_item(
        session,
        company_id=company_id,
        code=code,
        item_group=ItemGroup.RAW_MATERIAL,
        production_type=ProductionType.BUY,
        can_purchase=True,
        standard_cost=Decimal("40"),
    )
    return item.id


@pytest.fixture
async def plant(session, tenant, unique_code) -> SimpleNamespace:
    store = await seed_support.seed_warehouse(
        session, company_id=tenant.company_id, code=unique_code("WH")
    )
    return SimpleNamespace(
        store_id=store.id,
        item_id=await buy_item(session, tenant.company_id, unique_code("RM")),
        spare_item_id=await buy_item(session, tenant.company_id, unique_code("RM")),
    )


def manual(item_id: uuid.UUID, quantity: str = "40", days: int = 10) -> dict[str, Any]:
    return {
        "item_id": str(item_id),
        "quantity": quantity,
        "required_date": (today() + timedelta(days=days)).isoformat(),
    }


async def open_suggestion(
    session: AsyncSession, company_id: uuid.UUID, item_id: uuid.UUID, **overrides: Any
) -> uuid.UUID:
    """One PURCHASE suggestion on a completed run of its own, through the ORM."""
    run = MrpRun(
        company_id=company_id,
        code=f"MRP-{uuid.uuid4().hex[:10]}",
        horizon_days=30,
        status=MrpRunStatus.COMPLETED,
    )
    session.add(run)
    await session.flush()
    suggestion = MrpSuggestion(
        company_id=company_id,
        run_id=run.id,
        item_id=item_id,
        item_code="BULK-IGNORE",
        suggestion_type=MrpSuggestionType.PURCHASE,
        required_quantity=Decimal("40"),
        suggested_quantity=Decimal("40"),
        shortage_quantity=Decimal("40"),
        required_date=today() + timedelta(days=7),
        suggested_order_date=today(),
        **overrides,
    )
    session.add(suggestion)
    await session.flush()
    return suggestion.id


# -----------------------------------------------------------------------------
# PUT /mrp/schedule-settings
# -----------------------------------------------------------------------------


async def test_the_nightly_settings_are_created_then_replaced_with_an_audit_row_each_time(
    api_client, session, tenant, plant
):
    created = ok(
        await api_client.put(
            "/mrp/schedule-settings",
            json={
                "mrp_scheduled_enabled": False,
                "horizon_days": 30,
                "warehouse_ids": [str(plant.store_id)],
                "respect_safety_stock": False,
            },
        )
    )
    assert created == {
        "mrp_scheduled_enabled": False,
        "horizon_days": 30,
        "warehouse_ids": [str(plant.store_id)],
        "respect_safety_stock": False,
    }

    replaced = ok(await api_client.put("/mrp/schedule-settings", json={"horizon_days": 60}))
    assert replaced == {
        "mrp_scheduled_enabled": True,
        "horizon_days": 60,
        "warehouse_ids": None,
        "respect_safety_stock": True,
    }
    assert ok(await api_client.get("/mrp/schedule-settings")) == replaced

    [row] = (
        await session.scalars(
            select(MrpScheduleSettings).where(MrpScheduleSettings.company_id == tenant.company_id)
        )
    ).all()
    assert (row.horizon_days, row.warehouse_ids) == (60, None)
    trail = await audit_trail(session, "mrp_schedule_settings", row.id)
    assert [entry.action for entry in trail] == [AuditAction.CREATE, AuditAction.UPDATE]
    assert (trail[1].old_values["horizon_days"], trail[1].new_values["horizon_days"]) == (30, 60)


@pytest.mark.parametrize("store", ["another-company", "inactive"])
async def test_a_schedule_may_be_scoped_only_to_this_companys_active_stores(
    api_client, session, tenant, other_company, unique_code, store
):
    owner = other_company.id if store == "another-company" else tenant.company_id
    warehouse = await seed_support.seed_warehouse(
        session, company_id=owner, code=unique_code("WH"), is_active=store != "inactive"
    )

    error = refused(
        await api_client.put("/mrp/schedule-settings", json={"warehouse_ids": [str(warehouse.id)]}),
        422,
        "BUSINESS_RULE_VIOLATION",
    )

    assert error["details"]["warehouse_ids"] == [str(warehouse.id)]
    count = await session.scalar(
        select(func.count())
        .select_from(MrpScheduleSettings)
        .where(MrpScheduleSettings.company_id == tenant.company_id)
    )
    assert count == 0


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"horizon_days": 0}, "horizon_days"),
        ({"horizon_days": 5000}, "horizon_days"),
        ({"warehouse_ids": ["not-a-uuid"]}, "warehouse_ids.0"),
        ({"mrp_scheduled_enabled": "sometimes"}, "mrp_scheduled_enabled"),
    ],
    ids=["horizon-zero", "horizon-too-long", "bad-store-id", "not-a-flag"],
)
async def test_malformed_nightly_settings_are_field_errors(api_client, body, field):
    assert field in field_errors(await api_client.put("/mrp/schedule-settings", json=body))


# -----------------------------------------------------------------------------
# POST /mrp/runs
# -----------------------------------------------------------------------------


async def test_a_small_run_is_planned_inline_and_answers_with_the_completed_run(
    api_client, session, tenant, plant, broker
):
    data = ok(
        await api_client.post(
            "/mrp/runs",
            json={"horizon_days": 30, "manual_demand": [manual(plant.item_id)], "notes": "Weekly"},
        )
    )

    run_id = uuid.UUID(data["id"])
    assert (data["status"], data["mode"]) == ("COMPLETED", "SYNC")
    assert (data["demand_line_count"], data["suggestion_count"]) == (1, 1)
    assert data["status_url"].endswith(f"/mrp/runs/{run_id}")
    assert broker == []
    suggestions = (
        await session.execute(
            select(
                MrpSuggestion.item_id, MrpSuggestion.status, MrpSuggestion.suggested_quantity
            ).where(MrpSuggestion.run_id == run_id)
        )
    ).all()
    assert [(item, status) for item, status, _ in suggestions] == [
        (plant.item_id, MrpSuggestionStatus.OPEN)
    ]
    assert Decimal(suggestions[0][2]) == Decimal("40")
    demand = await session.scalar(
        select(func.count()).select_from(MrpDemand).where(MrpDemand.run_id == run_id)
    )
    assert demand == 1
    trail = await audit_trail(session, "mrp_run", run_id)
    assert trail[0].action == AuditAction.CREATE
    assert trail[0].new_values["mode"] == "SYNC"
    assert [
        (entry.old_values or {}).get("status")
        for entry in trail
        if (entry.new_values or {}).get("status") in {"RUNNING", "COMPLETED"}
    ] == ["QUEUED", "RUNNING"]


async def test_a_large_run_is_queued_and_answered_with_a_status_url(
    api_client, session, plant, broker, monkeypatch
):
    monkeypatch.setattr(settings, "mrp_sync_threshold_demand_lines", 0)

    response = await api_client.post("/mrp/runs", json={"manual_demand": [manual(plant.item_id)]})

    job = ok(response, 202)
    run_id = uuid.UUID(job["job_id"])
    assert job["status"] == "queued"
    assert job["status_url"].endswith(f"/mrp/runs/{run_id}")
    assert [(name, kwargs["run_id"]) for name, kwargs in broker] == [
        (mrp_service.MRP_TASK_NAME, str(run_id))
    ]
    stored = await session.get(MrpRun, run_id)
    assert stored is not None
    assert (stored.status, stored.mode, stored.demand_line_count) == (
        MrpRunStatus.QUEUED,
        MrpRunMode.ASYNC,
        1,
    )
    assert stored.celery_task_id is not None and stored.celery_task_id.startswith("task-")
    polled = ok(await api_client.get(relative(job["status_url"])))
    assert (polled["id"], polled["status"]) == (str(run_id), "QUEUED")

    # The worker holds the planning lock now: a second start is refused.
    refused(await api_client.post("/mrp/runs", json={}), 409, "MRP_RUN_IN_PROGRESS")


async def test_a_run_genuinely_in_flight_is_named_by_the_refusal(api_client, session, tenant):
    running = MrpRun(
        company_id=tenant.company_id,
        code=f"MRP-{uuid.uuid4().hex[:10]}",
        horizon_days=30,
        status=MrpRunStatus.RUNNING,
        started_at=utcnow(),
    )
    session.add(running)
    await session.flush()
    running_id = running.id

    error = refused(await api_client.post("/mrp/runs", json={}), 409, "MRP_RUN_IN_PROGRESS")

    assert error["details"]["run_id"] == str(running_id)
    count = await session.scalar(
        select(func.count()).select_from(MrpRun).where(MrpRun.company_id == tenant.company_id)
    )
    assert count == 1


async def test_a_run_abandoned_hours_ago_is_closed_off_and_planning_proceeds(
    api_client, session, tenant
):
    abandoned = MrpRun(
        company_id=tenant.company_id,
        code=f"MRP-{uuid.uuid4().hex[:10]}",
        horizon_days=30,
        status=MrpRunStatus.RUNNING,
        started_at=utcnow() - timedelta(hours=3),
    )
    session.add(abandoned)
    await session.flush()
    abandoned_id = abandoned.id

    data = ok(await api_client.post("/mrp/runs", json={}))

    assert (data["status"], data["demand_line_count"]) == ("COMPLETED", 0)
    assert data["warnings"]
    stale = (
        await session.execute(
            select(MrpRun.status, MrpRun.error_message).where(MrpRun.id == abandoned_id)
        )
    ).one()
    assert stale.status == MrpRunStatus.FAILED and stale.error_message
    [closed] = [
        entry
        for entry in await audit_trail(session, "mrp_run", abandoned_id)
        if entry.action == AuditAction.STATUS_CHANGE
    ]
    assert (closed.old_values["status"], closed.new_values["status"]) == ("RUNNING", "FAILED")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"horizon_days": 0}, "horizon_days"),
        ({"manual_demand": [{"item_id": "ITEM", "quantity": "0"}]}, "manual_demand.0.quantity"),
        (
            {"manual_demand": [{"item_id": "ITEM", "quantity": "5"}]},
            "manual_demand.0.required_date",
        ),
        ({"warehouse_ids": "everywhere"}, "warehouse_ids"),
    ],
    ids=["horizon-zero", "zero-demand", "undated-demand", "scope-not-a-list"],
)
async def test_a_malformed_run_request_is_a_field_error_and_starts_nothing(
    api_client, session, tenant, plant, body, field
):
    if "manual_demand" in body:
        body = {
            **body,
            "manual_demand": [
                {**line, "item_id": str(plant.item_id)} for line in body["manual_demand"]
            ],
        }

    assert field in field_errors(await api_client.post("/mrp/runs", json=body))
    count = await session.scalar(
        select(func.count()).select_from(MrpRun).where(MrpRun.company_id == tenant.company_id)
    )
    assert count == 0


@pytest.mark.parametrize("reference", ["demand-item", "run-scope", "demand-store"])
async def test_a_run_naming_another_companys_records_is_refused(
    api_client, session, tenant, other_company, plant, unique_code, reference
):
    their_item = await buy_item(session, other_company.id, unique_code("RM"))
    their_store = await seed_support.seed_warehouse(
        session, company_id=other_company.id, code=unique_code("WH")
    )
    body: dict[str, Any] = {
        "demand-item": {"manual_demand": [manual(their_item)]},
        "run-scope": {"warehouse_ids": [str(their_store.id)]},
        "demand-store": {
            "manual_demand": [{**manual(plant.item_id), "warehouse_id": str(their_store.id)}]
        },
    }[reference]

    response = await api_client.post("/mrp/runs", json=body)

    refused(response, 404, "NOT_FOUND")
    count = await session.scalar(
        select(func.count()).select_from(MrpRun).where(MrpRun.company_id == tenant.company_id)
    )
    assert count == 0


@pytest.mark.parametrize("state", ["inactive", "retired"])
async def test_a_run_may_still_be_scoped_to_this_companys_closed_stores(
    api_client, session, tenant, plant, unique_code, state
):
    """A nightly scope saved while a store was live must not fail once it closes."""
    closed = await seed_support.seed_warehouse(
        session,
        company_id=tenant.company_id,
        code=unique_code("WH"),
        **({"is_active": False} if state == "inactive" else {"deleted_at": utcnow()}),
    )

    data = ok(
        await api_client.post(
            "/mrp/runs",
            json={"warehouse_ids": [str(plant.store_id), str(closed.id)]},
        )
    )

    assert data["status"] == "COMPLETED"
    assert data["warehouse_ids"] == [str(plant.store_id), str(closed.id)]


# -----------------------------------------------------------------------------
# POST /mrp/suggestions/bulk-ignore
# -----------------------------------------------------------------------------


async def test_open_suggestions_are_ignored_with_one_reason_and_an_audit_row_each(
    api_client, session, tenant, plant
):
    first = await open_suggestion(session, tenant.company_id, plant.item_id)
    second = await open_suggestion(session, tenant.company_id, plant.spare_item_id)

    data = ok(
        await api_client.post(
            "/mrp/suggestions/bulk-ignore",
            json={"suggestion_ids": [str(first), str(second)], "reason": REASON},
        )
    )

    assert data["ignored_count"] == 2
    assert data["ignored_ids"] == [str(first), str(second)]
    assert data["skipped_ids"] == []
    stored = (
        await session.execute(
            select(MrpSuggestion.status, MrpSuggestion.ignored_reason).where(
                MrpSuggestion.id.in_([first, second])
            )
        )
    ).all()
    assert [tuple(row) for row in stored] == [(MrpSuggestionStatus.IGNORED, REASON)] * 2
    for suggestion_id in (first, second):
        [entry] = await audit_trail(session, "mrp_suggestion", suggestion_id)
        assert entry.action == AuditAction.STATUS_CHANGE
        assert (entry.old_values["status"], entry.new_values["status"]) == ("OPEN", "IGNORED")
        assert entry.context["reason"] == REASON


async def test_suggestions_no_longer_open_or_not_this_companys_are_skipped_not_failed(
    api_client, session, tenant, other_company, plant, unique_code
):
    still_open = await open_suggestion(session, tenant.company_id, plant.item_id)
    converted = await open_suggestion(
        session,
        tenant.company_id,
        plant.spare_item_id,
        status=MrpSuggestionStatus.CONVERTED,
        converted_to_type="PURCHASE_REQUISITION",
        converted_to_id=uuid.uuid4(),
        converted_at=utcnow(),
    )
    theirs = await open_suggestion(
        session, other_company.id, await buy_item(session, other_company.id, unique_code("RM"))
    )
    unknown = uuid.uuid4()
    asked = [still_open, converted, theirs, unknown]

    data = ok(
        await api_client.post(
            "/mrp/suggestions/bulk-ignore",
            json={"suggestion_ids": [str(value) for value in asked], "reason": REASON},
        )
    )

    assert data["ignored_ids"] == [str(still_open)]
    assert data["skipped_ids"] == [str(converted), str(theirs), str(unknown)]
    statuses = dict(
        (
            await session.execute(
                select(MrpSuggestion.id, MrpSuggestion.status).where(
                    MrpSuggestion.id.in_([converted, theirs])
                )
            )
        ).all()
    )
    assert statuses == {
        converted: MrpSuggestionStatus.CONVERTED,
        theirs: MrpSuggestionStatus.OPEN,
    }
    assert await audit_trail(session, "mrp_suggestion", theirs) == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"suggestion_ids": [], "reason": REASON}, "suggestion_ids"),
        ({"suggestion_ids": ["OPEN"]}, "reason"),
        ({"suggestion_ids": ["OPEN"], "reason": "no"}, "reason"),
        ({"suggestion_ids": ["not-a-uuid"], "reason": REASON}, "suggestion_ids.0"),
    ],
    ids=["none-named", "no-reason", "short-reason", "bad-id"],
)
async def test_a_malformed_bulk_ignore_is_a_field_error_and_ignores_nothing(
    api_client, session, tenant, plant, body, field
):
    suggestion_id = await open_suggestion(session, tenant.company_id, plant.item_id)
    body = {
        **body,
        "suggestion_ids": [
            str(suggestion_id) if value == "OPEN" else value for value in body["suggestion_ids"]
        ],
    }

    assert field in field_errors(await api_client.post("/mrp/suggestions/bulk-ignore", json=body))
    status = await session.scalar(
        select(MrpSuggestion.status).where(MrpSuggestion.id == suggestion_id)
    )
    assert status == MrpSuggestionStatus.OPEN
