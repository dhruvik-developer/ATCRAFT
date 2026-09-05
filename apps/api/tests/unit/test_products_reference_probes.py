"""The item master's cross-module references are checked in the caller's
tenant, not merely against the foreign key.

``item_vendor_links.vendor_id`` and ``items.default_warehouse_id`` point at
globally unique ids, so a uuid belonging to another company's vendor or
warehouse satisfies the constraint. These tests compile the probe statements
and check the three predicates that make the reference safe.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.core.deps import TenantContext
from src.modules.products.repository import ItemRepository

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value: bool) -> None:
        self.value = value

    def scalar(self) -> bool:
        return self.value


class _Session:
    def __init__(self, value: bool) -> None:
        self.value = value
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.value)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.fixture
def tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


async def test_vendor_probe_is_scoped_to_the_tenant_and_to_live_rows(
    tenant: TenantContext,
) -> None:
    session = _Session(value=False)

    found = await ItemRepository(session, tenant).vendor_exists(uuid.uuid4())  # type: ignore[arg-type]

    assert found is False
    sql = _sql(session.statements[0])
    assert "EXISTS" in sql
    assert "vendors.id = " in sql
    assert "vendors.company_id = " in sql
    assert "vendors.deleted_at IS NULL" in sql


async def test_warehouse_probe_is_scoped_to_the_tenant_and_to_live_rows(
    tenant: TenantContext,
) -> None:
    session = _Session(value=True)

    found = await ItemRepository(session, tenant).warehouse_exists(uuid.uuid4())  # type: ignore[arg-type]

    assert found is True
    sql = _sql(session.statements[0])
    assert "warehouses.id = " in sql
    assert "warehouses.company_id = " in sql
    assert "warehouses.deleted_at IS NULL" in sql


async def test_the_probe_binds_the_tenant_from_the_context_not_the_caller(
    tenant: TenantContext,
) -> None:
    session = _Session(value=True)
    vendor_id = uuid.uuid4()

    await ItemRepository(session, tenant).vendor_exists(vendor_id)  # type: ignore[arg-type]

    params = session.statements[0].compile(dialect=postgresql.dialect()).params
    assert tenant.company_id in params.values()
    assert vendor_id in params.values()
