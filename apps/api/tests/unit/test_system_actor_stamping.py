"""A scheduled job's placeholder actor never reaches an actor column.

``TenantContext`` needs a user id, and a beat sweep has nobody behind it, so the
sweeps run as ``SYSTEM_ACTOR_ID`` - the nil UUID. Every actor column
(``created_by_id``, ``updated_by_id``, ``released_by_id``) is a foreign key to
``users``, so storing that placeholder fails the whole operation. The allocation
top-up reserves and releases stock on a tenant's behalf, which is exactly the
path that did: these pin the one helper every writer goes through, and the two
writers almost everything else goes through.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest

from src.common.repository import BaseRepository
from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.core.ids import SYSTEM_ACTOR_ID, persisted_actor
from src.modules.allocation import service as allocation
from src.modules.audit.models import AuditAction
from src.modules.audit.service import record_audit
from src.modules.customers.models import Customer

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None


class _CustomerRepository(BaseRepository[Customer]):
    model = Customer


def test_a_person_is_stored_as_themselves_and_the_system_as_nobody() -> None:
    person = uuid.uuid4()
    assert persisted_actor(person) == person
    assert persisted_actor(SYSTEM_ACTOR_ID) is None
    assert persisted_actor(None) is None


def test_the_placeholder_is_the_nil_uuid_and_cannot_be_a_real_account() -> None:
    assert uuid.UUID(int=0) == SYSTEM_ACTOR_ID
    # Real ids are UUIDv7; the nil UUID has no version at all.
    assert SYSTEM_ACTOR_ID.version is None


def test_the_notifications_module_shares_the_one_placeholder() -> None:
    from src.modules.notifications.repository import SYSTEM_ACTOR_ID as NOTIFICATION_ACTOR

    assert NOTIFICATION_ACTOR is SYSTEM_ACTOR_ID


def test_a_repository_insert_by_the_system_records_no_creator() -> None:
    session = _Session()
    repo = _CustomerRepository(
        session, TenantContext(company_id=uuid.uuid4(), user_id=SYSTEM_ACTOR_ID)
    )  # type: ignore[arg-type]

    row = repo.add(Customer(code="C1", name="Customer"))
    assert row.created_by_id is None
    repo.touch(row)
    assert row.updated_by_id is None


def test_a_repository_insert_by_a_person_records_them() -> None:
    person = uuid.uuid4()
    repo = _CustomerRepository(_Session(), TenantContext(company_id=uuid.uuid4(), user_id=person))  # type: ignore[arg-type]

    row = repo.add(Customer(code="C1", name="Customer"))
    assert row.created_by_id == person
    repo.touch(row)
    assert row.updated_by_id == person


def test_an_audit_row_written_by_the_system_has_no_author() -> None:
    session = _Session()
    entry = asyncio.run(
        record_audit(
            session,  # type: ignore[arg-type]
            company_id=uuid.uuid4(),
            action=AuditAction.RESERVATION_CHANGE,
            entity_type="sales_order",
            summary="Allocated newly available stock",
            user_id=SYSTEM_ACTOR_ID,
            flush=False,
        )
    )
    assert entry.created_by_id is None
    assert session.added == [entry]


def test_a_serial_drawdown_must_name_one_distinct_unit_per_unit() -> None:
    """Refused before anything is locked, so a miscounted line moves nothing."""
    unit = uuid.uuid4()
    for serials, quantity in (([unit], "2"), ([unit, unit], "2")):
        with pytest.raises(BusinessRuleError, match="exactly one distinct unit"):
            asyncio.run(
                allocation.consume(
                    _Session(),  # type: ignore[arg-type]
                    TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4()),
                    reservation_id=uuid.uuid4(),
                    quantity=Decimal(quantity),
                    reference_type="dispatch",
                    reference_id=uuid.uuid4(),
                    reference_code="DSP/2026-27/00001",
                    serial_ids=serials,
                )
            )
