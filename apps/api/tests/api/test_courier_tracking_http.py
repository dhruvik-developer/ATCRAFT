"""Courier routes enforce pagination, tenant scope and webhook authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clock import today, utcnow
from src.core.config import settings
from src.core.errors import NotFoundError
from src.core.security import encrypt_courier_webhook_secret, hash_secret_token
from src.modules.courier_tracking import service as courier_service
from src.modules.courier_tracking.models import (
    CourierAccount,
    CourierProvider,
    CourierTrackingEvent,
)
from src.modules.dispatch.models import Dispatch, DispatchStatus, DispatchType
from src.modules.organization.models import Company
from src.modules.vendors.models import Transporter, TransporterType
from tests.seed_support import seed_customer, seed_warehouse

pytestmark = pytest.mark.integration


async def _consignment(
    session: AsyncSession,
    company_id: uuid.UUID,
    *,
    tracking_id: str | None,
    transporter_id: uuid.UUID | None = None,
) -> Dispatch:
    """A sales dispatch on the road: the least a manual refresh has to find."""
    suffix = uuid.uuid4().hex[:8].upper()
    warehouse = await seed_warehouse(session, company_id=company_id, code=f"WH-{suffix}")
    customer = await seed_customer(session, company_id=company_id)
    dispatch = Dispatch(
        company_id=company_id,
        code=f"DSP-{suffix}",
        dispatch_type=DispatchType.SALES,
        customer_id=customer.id,
        warehouse_id=warehouse.id,
        status=DispatchStatus.DISPATCHED,
        dispatch_date=today(),
        dispatched_at=utcnow(),
        tracking_id=tracking_id,
        transporter_id=transporter_id,
    )
    session.add(dispatch)
    await session.flush()
    return dispatch


async def _shiprocket_account(
    api_client: AsyncClient, code: str, email: str, *, transporter_ids: list[uuid.UUID]
) -> uuid.UUID:
    created = await api_client.post(
        "/integrations/courier/accounts",
        json={
            "code": code,
            "provider": "SHIPROCKET",
            "display_name": f"Shiprocket {code}",
            "credentials": {"email": email, "password": f"{code}-password"},
            "transporter_ids": [str(value) for value in transporter_ids],
        },
    )
    assert created.status_code == 201, created.text
    return uuid.UUID(created.json()["data"]["id"])


@pytest.mark.asyncio
async def test_account_list_is_paginated_and_tenant_scoped(
    api_client: AsyncClient,
    session: AsyncSession,
    company: Company,
    other_company: Company,
) -> None:
    session.add_all(
        [
            CourierAccount(
                id=uuid.uuid4(),
                company_id=company.id,
                code=f"LOCAL-{index}",
                provider=CourierProvider.GENERIC_WEBHOOK,
                display_name=f"Local courier {index}",
            )
            for index in range(2)
        ]
        + [
            CourierAccount(
                id=uuid.uuid4(),
                company_id=other_company.id,
                code="OTHER-1",
                provider=CourierProvider.GENERIC_WEBHOOK,
                display_name="Other tenant courier",
            )
        ]
    )
    await session.flush()

    response = await api_client.get("/integrations/courier/accounts?limit=1")

    assert response.status_code == 200
    result = response.json()
    assert len(result["data"]) == 1
    assert result["pagination"]["total"] == 2
    assert result["pagination"]["has_next"] is True
    assert all(account["code"].startswith("LOCAL-") for account in result["data"])


@pytest.mark.asyncio
async def test_each_account_change_names_what_changed(api_client: AsyncClient) -> None:
    created = await api_client.post(
        "/integrations/courier/accounts",
        json={"code": "GEN-MSG", "provider": "GENERIC_WEBHOOK", "display_name": "Generic"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["message"] == "Courier account created"
    account = f"/integrations/courier/accounts/{created.json()['data']['id']}"

    updated = await api_client.patch(account, json={"display_name": "Generic courier"})
    rotated = await api_client.post(f"{account}/rotate-webhook")
    tested = await api_client.post(f"{account}/test")

    assert [response.status_code for response in (updated, rotated, tested)] == [200] * 3
    assert updated.json()["message"] == "Courier account updated"
    assert rotated.json()["message"] == "Webhook secret reset"
    assert tested.json()["message"] == "Courier connection verified"


@pytest.mark.asyncio
async def test_an_unknown_account_or_consignment_is_named_once_in_its_404(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 404 names the record in the shared sentence - not 'not found. not found'."""
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    missing = uuid.uuid4()
    account = f"/integrations/courier/accounts/{missing}"

    responses = {
        "Courier account": [
            await api_client.patch(account, json={"display_name": "Generic courier"}),
            await api_client.post(f"{account}/rotate-webhook"),
            await api_client.post(f"{account}/test"),
        ],
        "Tracked dispatch": [
            await api_client.post(
                "/integrations/courier/refresh", json={"dispatch_id": str(missing)}
            )
        ],
        "Tracked inbound shipment": [
            await api_client.post(
                "/integrations/courier/refresh", json={"shipment_id": str(missing)}
            )
        ],
    }

    for entity, answers in responses.items():
        for response in answers:
            assert response.status_code == 404, response.text
            assert response.json()["error"]["message"] == NotFoundError(entity, missing).message


@pytest.mark.asyncio
async def test_a_refresh_polls_through_the_transporters_account_else_the_oldest(
    api_client: AsyncClient,
    session: AsyncSession,
    company: Company,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consignment's carrier picks the account; a consignment with none uses the oldest.

    Both accounts are created in one test transaction, so their ``created_at``
    is the same instant and "oldest" rests on the id tiebreak. The event the
    courier reports lands on the dispatch and moves it on, one step further
    than the poll itself.
    """
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    carrier = Transporter(
        company_id=company.id,
        code="CR-REFRESH",
        name="Refresh courier",
        transporter_type=TransporterType.COURIER,
    )
    session.add(carrier)
    await session.flush()
    oldest = await _shiprocket_account(
        api_client, "SR-OLDEST", "oldest@example.com", transporter_ids=[]
    )
    linked = await _shiprocket_account(
        api_client, "SR-LINKED", "linked@example.com", transporter_ids=[carrier.id]
    )
    carried = await _consignment(
        session, company.id, tracking_id="AWB-CARRIED-1", transporter_id=carrier.id
    )
    unassigned = await _consignment(session, company.id, tracking_id="AWB-UNASSIGNED-1")
    carried_id, unassigned_id = carried.id, unassigned.id

    polled: list[tuple[str, str]] = []

    async def login(email: str, password: str) -> str:
        return f"token:{email}"

    async def track(tracking_number: str, token: str) -> dict[str, Any]:
        polled.append((tracking_number, token))
        activity = {
            "id": f"activity:{tracking_number}",
            "activity": "In Transit",
            "date": utcnow().isoformat(),
            "location": "Pune hub",
        }
        return {
            "tracking_data": {
                "shipment_track": [{"awb_code": tracking_number}],
                "shipment_track_activities": [activity],
            }
        }

    monkeypatch.setattr(courier_service, "shiprocket_login", login)
    monkeypatch.setattr(courier_service, "shiprocket_track", track)

    answers = [
        await api_client.post("/integrations/courier/refresh", json={"dispatch_id": str(target)})
        for target in (carried_id, unassigned_id)
    ]

    for answer in answers:
        assert answer.status_code == 200, answer.text
        assert answer.json()["data"] == {"events_recorded": 1}
    assert polled == [
        ("AWB-CARRIED-1", "token:linked@example.com"),
        ("AWB-UNASSIGNED-1", "token:oldest@example.com"),
    ]
    events = (
        await session.execute(
            select(CourierTrackingEvent.dispatch_id, CourierTrackingEvent.courier_account_id).where(
                CourierTrackingEvent.company_id == company.id
            )
        )
    ).all()
    assert {tuple(row) for row in events} == {(carried_id, linked), (unassigned_id, oldest)}
    statuses = (
        await session.execute(
            select(Dispatch.status).where(Dispatch.id.in_([carried_id, unassigned_id]))
        )
    ).scalars()
    assert set(statuses) == {DispatchStatus.IN_TRANSIT}


@pytest.mark.asyncio
async def test_a_refresh_sees_only_this_companys_tracked_consignments(
    api_client: AsyncClient,
    session: AsyncSession,
    company: Company,
    other_company: Company,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another company's consignment and an untracked one are both "not found".

    Neither reaches the account lookup or the courier: the refusal comes from
    the tenant-scoped read, before anything leaves the building.
    """
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    untracked = await _consignment(session, company.id, tracking_id=None)
    foreign = await _consignment(session, other_company.id, tracking_id="AWB-FOREIGN-1")
    targets = [untracked.id, foreign.id]

    async def track(tracking_number: str, token: str) -> dict[str, Any]:
        raise AssertionError(f"{tracking_number} was polled although the refresh cannot see it")

    monkeypatch.setattr(courier_service, "shiprocket_track", track)

    for target in targets:
        response = await api_client.post(
            "/integrations/courier/refresh", json={"dispatch_id": str(target)}
        )
        assert response.status_code == 404, response.text
        assert (
            response.json()["error"]["message"] == NotFoundError("Tracked dispatch", target).message
        )


@pytest.mark.asyncio
async def test_a_refresh_with_no_shiprocket_account_says_so(
    api_client: AsyncClient,
    session: AsyncSession,
    company: Company,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    tracked = await _consignment(session, company.id, tracking_id="AWB-NO-ACCOUNT-1")

    response = await api_client.post(
        "/integrations/courier/refresh", json={"dispatch_id": str(tracked.id)}
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["message"] == (
        "No active Shiprocket account is configured for this company."
    )


@pytest.mark.asyncio
async def test_generic_webhook_verifies_signature_and_deduplicates_events(
    api_client: AsyncClient,
    session: AsyncSession,
    company: Company,
    tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    token = "test-courier-token"
    secret = "test-courier-webhook-secret"
    account = CourierAccount(
        id=uuid.uuid4(),
        company_id=company.id,
        code="GENERIC-1",
        provider=CourierProvider.GENERIC_WEBHOOK,
        display_name="Generic courier",
        webhook_token_hash=hash_secret_token(token),
        webhook_secret_ciphertext=encrypt_courier_webhook_secret(secret),
    )
    session.add(account)
    await session.flush()

    event_time = datetime.now(UTC).replace(microsecond=0).isoformat()
    body = json.dumps(
        {
            "events": [
                {
                    "event_id": "courier-event-1",
                    "tracking_number": "AWB-1",
                    "status": "In Transit",
                    "event_time": event_time,
                },
                {
                    "event_id": "courier-event-2",
                    "tracking_number": "AWB-1",
                    "status": "In Transit",
                    "event_time": event_time,
                },
            ]
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()

    headers = {
        "X-Courier-Timestamp": timestamp,
        "X-Courier-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }
    valid = await api_client.post(
        f"/integrations/courier/webhook/{token}", content=body, headers=headers
    )
    repeated = await api_client.post(
        f"/integrations/courier/webhook/{token}", content=body, headers=headers
    )

    assert valid.status_code == 202
    assert valid.json()["data"]["events_recorded"] == 1
    assert valid.json()["message"] == "Webhook accepted"
    assert repeated.status_code == 202
    assert repeated.json()["data"]["events_recorded"] == 0

    bad_signature = await api_client.post(
        f"/integrations/courier/webhook/{token}",
        content=body,
        headers={**headers, "X-Courier-Signature": "sha256=" + "0" * 64},
    )
    assert bad_signature.status_code == 401
    unknown_token = await api_client.post(
        f"/integrations/courier/webhook/{token}-unknown", content=body, headers=headers
    )
    assert unknown_token.status_code == 404

    events = list(
        (
            await session.execute(
                select(CourierTrackingEvent).where(
                    CourierTrackingEvent.company_id == tenant.company_id,
                    CourierTrackingEvent.courier_account_id == account.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
