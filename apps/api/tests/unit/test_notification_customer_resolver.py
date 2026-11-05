from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import TenantContext
from src.modules.notifications.repository import NotificationRepository

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rows: list[uuid.UUID]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[uuid.UUID]:
        return self._rows


class _Session:
    def __init__(self, rows: list[uuid.UUID]) -> None:
        self.rows = rows
        self.statement: Any | None = None

    async def execute(self, statement: Any) -> _Result:
        self.statement = statement
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_customer_recipient_resolver_is_tenant_scoped_and_permission_filtered() -> None:
    company_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session = _Session([user_id])
    repository = NotificationRepository(
        session,
        TenantContext(company_id=company_id, user_id=uuid.uuid4()),
    )

    resolved = await repository.user_ids_for_customer(customer_id, permission="sales.view")

    assert resolved == [user_id]
    assert session.statement is not None
    compiled = session.statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "users.company_id =" in sql
    assert "users.customer_id =" in sql
    assert "users.is_active IS true" in sql
    assert "users.deleted_at IS NULL" in sql
    assert "users.is_superuser IS true" in sql
    assert "users.denied_permissions" in sql
    assert company_id in compiled.params.values()
    assert customer_id in compiled.params.values()
    assert "sales.view" in compiled.params.values()
    assert "LIMIT" in sql
