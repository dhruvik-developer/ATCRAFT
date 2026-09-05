"""Expiring a reservation writes the lifecycle audit row every other move does.

Release, consume and reallocation each record a ``record_status_change`` row -
``old_values={"status": ...}``, ``new_values={"status": ...}``,
``changed_fields=["status"]`` - when a reservation's status moves. Expiry used
to write only a free-text summary, so an auditor filtering the log on the
status field saw releases but never expiries. The sweep is driven here with its
repositories and audit writers stubbed, so the test proves the row is written
without a database.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

import src.modules.allocation.service as allocation_service
from src.core.clock import utcnow
from src.core.deps import TenantContext
from src.modules.allocation.models import (
    DemandType,
    ReservationStatus,
    ReservationType,
    StockReservation,
)
from src.modules.allocation.repository import StockReservationRepository
from src.modules.allocation.service import (
    AUDIT_ENTITY_RESERVATION,
    expire_stale_reservations,
)
from src.modules.audit.models import AuditAction
from src.modules.inventory.models import InventoryBalance
from src.modules.inventory.repository import BalanceGrain
from src.modules.inventory.service import MovementEngine

pytestmark = pytest.mark.unit

REASON = "Reservation passed its expiry date"


class _Session:
    """The only session method the sweep calls directly."""

    def __init__(self) -> None:
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1


@asynccontextmanager
async def _no_transaction(session: Any) -> AsyncIterator[Any]:
    yield session


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


def _reservation(tenant: TenantContext, grain: BalanceGrain) -> StockReservation:
    return StockReservation(
        id=uuid.uuid4(),
        company_id=tenant.company_id,
        item_id=grain.item_id,
        warehouse_id=grain.warehouse_id,
        location_id=grain.location_id,
        batch_id=grain.batch_id,
        demand_type=DemandType.SALES_ORDER,
        demand_id=uuid.uuid4(),
        demand_code="SO/2026-27/00007",
        reservation_type=ReservationType.HARD,
        quantity=Decimal("10"),
        consumed_quantity=Decimal("0"),
        released_quantity=Decimal("0"),
        status=ReservationStatus.ACTIVE,
        expires_at=utcnow() - timedelta(days=1),
    )


def _balance(tenant: TenantContext, grain: BalanceGrain, reserved: Decimal) -> InventoryBalance:
    return InventoryBalance(
        id=uuid.uuid4(),
        company_id=tenant.company_id,
        item_id=grain.item_id,
        warehouse_id=grain.warehouse_id,
        on_hand_quantity=Decimal("25"),
        reserved_quantity=reserved,
    )


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Capture every audit write the sweep makes, keyed by writer."""
    calls: dict[str, list[dict[str, Any]]] = {"audit": [], "status": []}

    async def _record_audit(session: Any, **kwargs: Any) -> None:
        calls["audit"].append(kwargs)

    async def _record_status_change(session: Any, **kwargs: Any) -> None:
        calls["status"].append(kwargs)

    monkeypatch.setattr(allocation_service, "record_audit", _record_audit)
    monkeypatch.setattr(allocation_service, "record_status_change", _record_status_change)
    monkeypatch.setattr(allocation_service, "transaction", _no_transaction)
    return calls


def _stub_rows(
    monkeypatch: pytest.MonkeyPatch,
    reservation: StockReservation,
    grain: BalanceGrain,
    balance: InventoryBalance,
) -> None:
    async def _due(self: Any, moment: datetime, *, limit: int) -> list[StockReservation]:
        return [reservation]

    async def _lock_reservations(
        self: Any, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, StockReservation]:
        return {reservation.id: reservation}

    async def _lock(self: Any, grains: list[BalanceGrain]) -> dict[BalanceGrain, InventoryBalance]:
        return {grain: balance}

    monkeypatch.setattr(StockReservationRepository, "due_for_expiry", _due)
    monkeypatch.setattr(StockReservationRepository, "lock_reservations", _lock_reservations)
    monkeypatch.setattr(MovementEngine, "lock", _lock)


async def test_expiry_records_the_status_transition(
    monkeypatch: pytest.MonkeyPatch, audit: dict[str, list[dict[str, Any]]]
) -> None:
    tenant = _tenant()
    grain = BalanceGrain(
        item_id=uuid.uuid4(), warehouse_id=uuid.uuid4(), location_id=None, batch_id=None
    )
    reservation = _reservation(tenant, grain)
    balance = _balance(tenant, grain, reserved=Decimal("10"))
    _stub_rows(monkeypatch, reservation, grain, balance)

    expired = await expire_stale_reservations(_Session(), tenant, reason=REASON)

    assert expired == [reservation.id]
    assert reservation.status is ReservationStatus.EXPIRED
    assert balance.reserved_quantity == Decimal("0")

    assert len(audit["status"]) == 1
    row = audit["status"][0]
    assert row["entity_type"] == AUDIT_ENTITY_RESERVATION
    assert row["entity_id"] == reservation.id
    assert row["entity_code"] == reservation.demand_code
    assert row["from_status"] == "ACTIVE"
    assert row["to_status"] == "EXPIRED"
    assert row["action"] is AuditAction.RESERVATION_CHANGE
    assert row["reason"] == REASON
    # The same actor on both rows, so the summary and the lifecycle entry can
    # never disagree about who expired it.
    assert row["user_id"] == tenant.user_id
    assert [entry["user_id"] for entry in audit["audit"]] == [tenant.user_id]


async def test_a_partially_consumed_reservation_reports_its_real_prior_status(
    monkeypatch: pytest.MonkeyPatch, audit: dict[str, list[dict[str, Any]]]
) -> None:
    tenant = _tenant()
    grain = BalanceGrain(
        item_id=uuid.uuid4(), warehouse_id=uuid.uuid4(), location_id=None, batch_id=None
    )
    reservation = _reservation(tenant, grain)
    reservation.status = ReservationStatus.PARTIALLY_CONSUMED
    reservation.consumed_quantity = Decimal("4")
    balance = _balance(tenant, grain, reserved=Decimal("6"))
    _stub_rows(monkeypatch, reservation, grain, balance)

    await expire_stale_reservations(_Session(), tenant, reason=REASON)

    assert audit["status"][0]["from_status"] == "PARTIALLY_CONSUMED"
    assert audit["status"][0]["to_status"] == "EXPIRED"
    assert balance.reserved_quantity == Decimal("0")


async def test_a_skipped_inconsistent_row_writes_no_lifecycle_entry(
    monkeypatch: pytest.MonkeyPatch, audit: dict[str, list[dict[str, Any]]]
) -> None:
    """A row the sweep refuses to expire must not claim to have expired."""
    tenant = _tenant()
    grain = BalanceGrain(
        item_id=uuid.uuid4(), warehouse_id=uuid.uuid4(), location_id=None, batch_id=None
    )
    reservation = _reservation(tenant, grain)
    # The balance holds less than the reservation claims: out of step, skipped.
    balance = _balance(tenant, grain, reserved=Decimal("3"))
    _stub_rows(monkeypatch, reservation, grain, balance)

    expired = await expire_stale_reservations(_Session(), tenant, reason=REASON)

    assert expired == []
    assert reservation.status is ReservationStatus.ACTIVE
    assert audit["status"] == []
    assert audit["audit"] == []
