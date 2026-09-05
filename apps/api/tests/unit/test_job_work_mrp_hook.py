"""Planning's conversion hook into job work refuses, in job work's own words.

``mrp.service.CONVERSION_HOOKS`` names ``create_order_from_mrp`` for the
JOB_WORK_ORDER target. A job work order needs a vendor and a process, which a
planning run does not choose, so the hook exists to say so itself - with the
same sentence MRP falls back to when the hook is absent - rather than to invent
a vendor. Either way the suggestion stays OPEN for the planner.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.deps import TenantContext
from src.core.errors import BusinessRuleError
from src.modules.job_work import service
from src.modules.mrp.schemas import ConversionTarget
from src.modules.mrp.service import CONVERSION_HOOKS, MANUAL_CONVERSION_REASONS

pytestmark = pytest.mark.unit


class _RefusingSession:
    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the refusal must not touch the database")


def _request() -> Any:
    return SimpleNamespace(
        suggestion_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        item_code="PLT-01",
        quantity=Decimal("40"),
        required_date=date(2026, 10, 1),
        suggested_order_date=date(2026, 9, 10),
        warehouse_ids=[],
        notes=None,
    )


def test_the_hook_is_the_one_planning_resolves() -> None:
    module_name, function_name = CONVERSION_HOOKS[ConversionTarget.JOB_WORK_ORDER]
    assert module_name == service.__name__
    assert getattr(service, function_name) is service.create_order_from_mrp


async def test_the_hook_refuses_with_the_same_sentence_planning_uses() -> None:
    request = _request()
    tenant = TenantContext(company_id=uuid.uuid4(), user_id=uuid.uuid4())

    with pytest.raises(BusinessRuleError) as exc:
        await service.create_order_from_mrp(_RefusingSession(), tenant, request)  # type: ignore[arg-type]

    assert exc.value.message == MANUAL_CONVERSION_REASONS[ConversionTarget.JOB_WORK_ORDER]
    assert exc.value.message == service.MRP_CONVERSION_REFUSED
    assert exc.value.details == {
        "suggestion_id": str(request.suggestion_id),
        "item_code": "PLT-01",
    }
