"""Courier routes enforce pagination, tenant scope and webhook authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.errors import NotFoundError
from src.core.security import encrypt_courier_webhook_secret, hash_secret_token
from src.modules.courier_tracking.models import (
    CourierAccount,
    CourierProvider,
    CourierTrackingEvent,
)
from src.modules.organization.models import Company

pytestmark = pytest.mark.integration


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
