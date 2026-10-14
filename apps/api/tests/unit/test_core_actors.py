"""The system actor: what a sweep with no principal stamps and audits as.

A beat sweep carries the nil UUID as its user, because ``TenantContext``
requires one. No ``users`` row has that id, so it must never reach a foreign
key: the repository base class stamps NULL instead, and the audit writer records
no user and the label "System". Without that, any sweep calling an ordinary
service - the quality owner reconciliation, the stale-reservation expiry - fails
on its first write.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.common.repository import BaseRepository
from src.core.actors import SYSTEM_ACTOR_ID, SYSTEM_ACTOR_LABEL, actor_id, is_system_actor
from src.core.deps import TenantContext, system_tenant
from src.modules.audit.models import AuditAction, AuditLog
from src.modules.audit.service import record_audit
from src.modules.notifications.repository import SYSTEM_ACTOR_ID as NOTIFICATIONS_SYSTEM_ACTOR
from src.modules.warehouses.models import Warehouse

pytestmark = pytest.mark.unit


class _Session:
    """Just enough of an ``AsyncSession`` to watch what is staged."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None


def test_the_system_actor_is_stamped_as_nobody() -> None:
    person = uuid.uuid4()
    assert actor_id(SYSTEM_ACTOR_ID) is None
    assert actor_id(None) is None
    assert actor_id(person) == person
    assert is_system_actor(SYSTEM_ACTOR_ID)
    assert not is_system_actor(person)


def test_a_sweep_scope_carries_the_system_actor_for_one_company() -> None:
    company = uuid.uuid4()
    tenant = system_tenant(company)
    assert tenant == TenantContext(company_id=company, user_id=SYSTEM_ACTOR_ID)
    # One placeholder for the whole application, not one per module.
    assert NOTIFICATIONS_SYSTEM_ACTOR is SYSTEM_ACTOR_ID


def test_a_row_written_by_a_sweep_names_no_creator_and_no_updater() -> None:
    session = _Session()
    repo: BaseRepository[Warehouse] = BaseRepository(session, system_tenant(uuid.uuid4()))  # type: ignore[arg-type]
    row = Warehouse(code="WH-SWEEP", name="Swept")

    repo.add(row)
    repo.touch(row)

    assert row.created_by_id is None
    assert row.updated_by_id is None


def test_a_row_written_by_a_person_still_names_them() -> None:
    person = uuid.uuid4()
    repo: BaseRepository[Warehouse] = BaseRepository(
        _Session(),  # type: ignore[arg-type]
        TenantContext(company_id=uuid.uuid4(), user_id=person),
    )
    row = Warehouse(code="WH-PERSON", name="Kept")

    repo.add(row)
    repo.touch(row)

    assert row.created_by_id == person
    assert row.updated_by_id == person


async def test_an_audit_row_written_by_a_sweep_says_system() -> None:
    session = _Session()

    entry = await record_audit(
        session,  # type: ignore[arg-type]
        company_id=uuid.uuid4(),
        action=AuditAction.STATUS_CHANGE,
        entity_type="assembly_order",
        user_id=SYSTEM_ACTOR_ID,
    )

    assert isinstance(entry, AuditLog)
    assert entry.created_by_id is None
    assert entry.actor_label == SYSTEM_ACTOR_LABEL


async def test_a_sweep_may_still_name_itself() -> None:
    entry = await record_audit(
        _Session(),  # type: ignore[arg-type]
        company_id=uuid.uuid4(),
        action=AuditAction.STATUS_CHANGE,
        entity_type="dispatch",
        user_id=SYSTEM_ACTOR_ID,
        actor_label="notifications.scan_dispatch_delays",
    )
    assert entry.actor_label == "notifications.scan_dispatch_delays"
