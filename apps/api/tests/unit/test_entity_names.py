"""One user-facing name per record, across every path that can name it.

``NotFoundError`` renders ``"{entity} not found: {id}"`` and
``InvalidStateTransitionError`` renders ``"{entity} cannot move from ..."``, so a
service that says "Work centre" in one message while ``get_or_404`` says
"WorkCenter" and the state machine says "WorkCenter" answers three spellings for
one record. The repositories in routing, sales and MRP now set
``BaseRepository.entity_label`` to the same constant their services and
machines use, so ``get_or_404`` names the record the way everything else does.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import NotFoundError
from src.modules.routing.repository import WORK_CENTER_ENTITY, WorkCenterRepository
from src.modules.routing.service import WORK_CENTER_LIFECYCLE
from src.modules.sales.repository import SALES_ORDER_ENTITY, SalesOrderRepository

pytestmark = pytest.mark.unit


class _NoRowSession:
    async def execute(self, statement: Any) -> Any:
        class _Result:
            def scalar_one_or_none(self) -> None:
                return None

        return _Result()


def _tenant() -> TenantContext:
    return TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())


@pytest.mark.parametrize(
    ("repository", "entity"),
    [
        (WorkCenterRepository, WORK_CENTER_ENTITY),
        (SalesOrderRepository, SALES_ORDER_ENTITY),
    ],
    ids=["work-centre", "sales-order"],
)
async def test_get_or_404_names_the_record_the_way_the_service_does(
    repository: Any, entity: str
) -> None:
    missing = uuid.uuid4()
    with pytest.raises(NotFoundError) as excinfo:
        await repository(_NoRowSession(), _tenant()).get_or_404(missing)
    assert excinfo.value.message == f"{entity} not found: {missing}"


def test_the_human_names_are_not_class_names() -> None:
    assert WORK_CENTER_ENTITY == "Work centre"
    assert SALES_ORDER_ENTITY == "Sales order"


def test_the_work_centre_state_machine_uses_the_same_name() -> None:
    """The transition message and the not-found message agree on the record."""
    assert WORK_CENTER_LIFECYCLE.entity == WORK_CENTER_ENTITY
