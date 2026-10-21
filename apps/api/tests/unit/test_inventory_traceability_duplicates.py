"""A duplicate batch or serial number is a 409, like every other duplicate.

``DUPLICATE_DOCUMENT`` reaches the client as 409 from ``DuplicateError`` and
from the integrity handler that catches the unique index when the check races.
The traceability service used to raise it as a 422 ``BusinessRuleError``, so a
client keying retry or refresh behaviour on the status saw two behaviours for
one condition. Driven with the repositories stubbed - no database.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import src.modules.inventory.service as inventory_service
from src.core.deps import TenantContext
from src.core.errors import DuplicateError, ErrorCode
from src.modules.inventory.schemas import StockBatchCreate, StockSerialCreate
from src.modules.inventory.service import TraceabilityService

pytestmark = pytest.mark.unit


@asynccontextmanager
async def _no_transaction(session: Any) -> AsyncIterator[Any]:
    yield session


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> TraceabilityService:
    monkeypatch.setattr(inventory_service, "transaction", _no_transaction)
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    return TraceabilityService(object(), tenant)


def _knows_item(
    monkeypatch: pytest.MonkeyPatch, service: TraceabilityService, item_id: uuid.UUID
) -> None:
    async def _facts(item_ids: Any) -> dict[uuid.UUID, Any]:
        return {item_id: SimpleNamespace(id=item_id, code="HNG-SS-100", is_serial_tracked=True)}

    monkeypatch.setattr(service.inventory, "item_facts", _facts)


async def test_a_second_batch_with_the_same_number_is_a_409(
    monkeypatch: pytest.MonkeyPatch, service: TraceabilityService
) -> None:
    item_id = uuid.uuid4()
    _knows_item(monkeypatch, service, item_id)

    async def _already_there(item: uuid.UUID, number: str) -> object:
        return object()

    monkeypatch.setattr(service.batches, "by_number", _already_there)

    with pytest.raises(DuplicateError) as exc:
        await service.create_batch(StockBatchCreate(item_id=item_id, batch_number="LOT-2026-014"))

    assert exc.value.status_code == 409
    assert exc.value.code is ErrorCode.DUPLICATE_DOCUMENT
    # Names the batch and the item, never an id.
    assert "LOT-2026-014" in exc.value.message
    assert "HNG-SS-100" in exc.value.message
    assert exc.value.details == {"batch_number": "LOT-2026-014"}


async def test_a_second_serial_with_the_same_number_is_a_409(
    monkeypatch: pytest.MonkeyPatch, service: TraceabilityService
) -> None:
    item_id = uuid.uuid4()
    _knows_item(monkeypatch, service, item_id)

    async def _already_there(item: uuid.UUID, number: str) -> object:
        return object()

    monkeypatch.setattr(service.serials, "by_number", _already_there)

    with pytest.raises(DuplicateError) as exc:
        await service.create_serial(StockSerialCreate(item_id=item_id, serial_number="SN-000451"))

    assert exc.value.status_code == 409
    assert exc.value.code is ErrorCode.DUPLICATE_DOCUMENT
    assert "SN-000451" in exc.value.message
    assert "HNG-SS-100" in exc.value.message
    assert exc.value.details == {"serial_number": "SN-000451"}
