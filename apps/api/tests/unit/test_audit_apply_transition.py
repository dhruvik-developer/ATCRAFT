"""``apply_transition``: the one shape six modules' status helpers wrote out.

Assembly, dispatch, job work, packing, sales and routing each moved a document
the same way - assert the edge, write the status, stamp the editor, flush, write
the status-change row - in a ``_transition`` of their own, so a change to that
shape had to be made six times. Each now binds its machine and audit name to
``audit.service.apply_transition``. These tests pin the order that matters: the
edge is asserted before anything changes, so a refused move leaves the row, the
session and the audit trail exactly as they were.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import InvalidStateTransitionError
from src.modules.audit import service as audit_service
from src.modules.audit.models import AuditAction
from src.modules.sales.models import SalesOrderStatus
from src.modules.sales.service import SALES_ORDER_LIFECYCLE

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self) -> None:
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1


class _Repository:
    def __init__(self) -> None:
        self.touched: list[Any] = []

    def touch(self, row: Any) -> Any:
        self.touched.append(row)
        return row


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every status-change row ``apply_transition`` asks for, by its keyword arguments."""
    rows: list[dict[str, Any]] = []

    async def record(session: Any, **values: Any) -> None:
        rows.append(values)

    monkeypatch.setattr(audit_service, "record_status_change", record)
    return rows


def _order(status: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), code="SO/2026-27/00001", status=status)


async def test_a_legal_move_writes_the_status_stamps_the_editor_and_audits_it(
    recorded: list[dict[str, Any]],
) -> None:
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    order, session, repository = _order("DRAFT"), _Session(), _Repository()

    await audit_service.apply_transition(
        session,  # type: ignore[arg-type]
        tenant,
        SALES_ORDER_LIFECYCLE,
        order,
        SalesOrderStatus.CONFIRMED,
        entity_type="sales_order",
        reason="Customer committed",
        repo=repository,  # type: ignore[arg-type]
        action=AuditAction.APPROVE,
    )

    assert order.status is SalesOrderStatus.CONFIRMED
    assert (repository.touched, session.flushes) == ([order], 1)
    assert recorded == [
        {
            "company_id": tenant.company_id,
            "entity_type": "sales_order",
            "entity_id": order.id,
            "entity_code": order.code,
            "from_status": "DRAFT",
            "to_status": "CONFIRMED",
            "user_id": tenant.user_id,
            "action": AuditAction.APPROVE,
            "reason": "Customer committed",
        }
    ]


async def test_a_refused_move_changes_nothing(recorded: list[dict[str, Any]]) -> None:
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())
    order, session, repository = _order("DRAFT"), _Session(), _Repository()

    with pytest.raises(InvalidStateTransitionError):
        await audit_service.apply_transition(
            session,  # type: ignore[arg-type]
            tenant,
            SALES_ORDER_LIFECYCLE,
            order,
            SalesOrderStatus.DISPATCHED,
            entity_type="sales_order",
            reason="Skipping ahead",
            repo=repository,  # type: ignore[arg-type]
        )

    assert order.status == "DRAFT"
    assert (repository.touched, session.flushes, recorded) == ([], 0, [])
