"""The scope policy agrees for database predicates and document destinations."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.common.warehouse_scope import assert_warehouse_scope, warehouse_scope
from src.core.deps import TenantContext
from src.core.errors import AuthorizationError
from src.modules.inventory.models import StockTransfer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("superuser,restricted", [(False, False), (False, True), (True, True)])
def test_all_transfer_endpoints_obey_one_scope_policy(superuser, restricted):
    allowed, outside = uuid.uuid4(), uuid.uuid4()
    tenant = TenantContext(
        company_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_superuser=superuser,
        allowed_warehouse_ids=(allowed,) if restricted else (),
    )
    predicates = warehouse_scope(
        tenant, StockTransfer.from_warehouse_id, StockTransfer.to_warehouse_id
    )
    if restricted and not superuser:
        assert len(predicates) == 2
        statement = select(StockTransfer.id).where(*predicates)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "stock_transfers.from_warehouse_id IN" in sql
        assert "stock_transfers.to_warehouse_id IN" in sql
        assert " AND " in sql
        with pytest.raises(AuthorizationError):
            assert_warehouse_scope(tenant, allowed, outside)
    else:
        assert predicates == ()
        assert_warehouse_scope(tenant, allowed, outside)
    assert_warehouse_scope(tenant, allowed)
