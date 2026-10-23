"""Representative master fixtures reuse existing identities and business services."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.demo_data import DemoContext, masters
from src.core.deps import Principal, TenantContext
from src.modules.products.models import ItemAttributeType

pytestmark = pytest.mark.unit


@pytest.fixture
def ctx() -> DemoContext:
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    tenant = TenantContext(company_id=company_id, user_id=user_id, is_superuser=True)
    principal = Principal(
        company_id=company_id,
        user_id=user_id,
        email="seed@example.test",
        full_name="Seed operator",
        is_superuser=True,
        permissions=frozenset(),
    )
    return DemoContext(
        cast(AsyncSession, Mock(spec=AsyncSession)), tenant, principal, company_id, user_id
    )


@pytest.mark.parametrize("existing", [False, True])
async def test_plant_uses_head_office_and_reuses_its_deterministic_identity(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    plant_id, branch_id = uuid.uuid4(), uuid.uuid4()
    service = SimpleNamespace(create_plant=AsyncMock(return_value=SimpleNamespace(id=plant_id)))
    monkeypatch.setattr(masters, "PlantService", Mock(return_value=service))
    monkeypatch.setattr(masters, "_branch_id", AsyncMock(return_value=branch_id))
    monkeypatch.setattr(
        masters,
        "find_by_code",
        AsyncMock(return_value=SimpleNamespace(id=plant_id) if existing else None),
    )

    await masters._seed_plant(ctx)

    assert ctx.id_of("plant", "DM-PLANT-RAJKOT") == plant_id
    assert service.create_plant.await_count == int(not existing)
    if not existing:
        payload = service.create_plant.await_args.args[0]
        assert payload.branch_id == branch_id
        assert payload.code == "DM-PLANT-RAJKOT"
    assert ctx.counts["masters"] == {"created": int(not existing), "existing": int(existing)}


@pytest.mark.parametrize(
    ("anchor", "start_month", "code", "start", "end"),
    [
        (date(2026, 10, 23), 4, "2026-27", date(2026, 4, 1), date(2027, 3, 31)),
        (date(2026, 3, 31), 4, "2025-26", date(2025, 4, 1), date(2026, 3, 31)),
        (date(2026, 10, 23), 1, "2026-27", date(2026, 1, 1), date(2026, 12, 31)),
        (date(2024, 2, 29), 3, "2023-24", date(2023, 3, 1), date(2024, 2, 29)),
    ],
)
async def test_current_financial_year_respects_company_calendar_and_inclusive_end(
    ctx: DemoContext,
    monkeypatch: pytest.MonkeyPatch,
    anchor: date,
    start_month: int,
    code: str,
    start: date,
    end: date,
) -> None:
    year_id = uuid.uuid4()
    ctx.session.scalar = AsyncMock(return_value=start_month)
    service = SimpleNamespace(
        repo=SimpleNamespace(overlapping_active=AsyncMock(return_value=None)),
        create_year=AsyncMock(return_value=SimpleNamespace(id=year_id)),
    )
    monkeypatch.setattr(masters, "FinancialYearService", Mock(return_value=service))
    monkeypatch.setattr(masters, "find_by_code", AsyncMock(return_value=None))

    await masters._seed_financial_year(ctx, anchor)

    payload = service.create_year.await_args.args[0]
    assert (payload.code, payload.start_date, payload.end_date) == (code, start, end)
    assert payload.is_active is True
    assert ctx.id_of("financial_year", code) == year_id
    service.repo.overlapping_active.assert_awaited_once_with(start, end)


@pytest.mark.parametrize("same_code", [True, False])
async def test_financial_year_reuses_existing_or_overlapping_period_without_reopening(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch, same_code: bool
) -> None:
    year_id = uuid.uuid4()
    existing = SimpleNamespace(id=year_id, code="2026-27" if same_code else "EXISTING-FY")
    ctx.session.scalar = AsyncMock(return_value=4)
    service = SimpleNamespace(
        repo=SimpleNamespace(overlapping_active=AsyncMock(return_value=existing)),
        create_year=AsyncMock(),
    )
    monkeypatch.setattr(masters, "FinancialYearService", Mock(return_value=service))
    monkeypatch.setattr(
        masters, "find_by_code", AsyncMock(return_value=existing if same_code else None)
    )

    await masters._seed_financial_year(ctx, date(2026, 10, 23))

    service.create_year.assert_not_awaited()
    assert ctx.id_of("financial_year", "2026-27") == year_id
    assert ctx.counts["masters"] == {"created": 0, "existing": 1}


@pytest.mark.parametrize("existing_kind", ["none", "code", "combination"])
async def test_variant_uses_existing_item_attributes_and_never_duplicates_a_combination(
    ctx: DemoContext, monkeypatch: pytest.MonkeyPatch, existing_kind: str
) -> None:
    item_id, finish_id, size_id, variant_id = (uuid.uuid4() for _ in range(4))
    ctx.remember("item", "DM-FG-LEVER-AB", item_id)
    ctx.remember("item_attribute", f"{ItemAttributeType.FINISH.value}:BLACK-NICKEL", finish_id)
    ctx.remember("item_attribute", f"{ItemAttributeType.SIZE.value}:52MM-ROSE", size_id)
    existing = SimpleNamespace(id=variant_id)
    result = Mock()
    result.scalar_one_or_none.return_value = existing if existing_kind == "combination" else None
    ctx.session.execute = AsyncMock(return_value=result)
    create = AsyncMock(return_value=SimpleNamespace(id=variant_id))
    monkeypatch.setattr(masters, "create_variant", create)
    monkeypatch.setattr(
        masters,
        "find_by_code",
        AsyncMock(return_value=existing if existing_kind == "code" else None),
    )

    await masters._seed_item_variant(ctx)

    assert ctx.id_of("item_variant", "DM-VAR-LEVER-BN") == variant_id
    assert create.await_count == int(existing_kind == "none")
    if existing_kind == "none":
        payload = create.await_args.args[3]
        assert create.await_args.args[2] == item_id
        assert (payload.finish_id, payload.size_id) == (finish_id, size_id)
        assert payload.price_delta == Decimal("75")
        assert payload.cost_delta == Decimal("20")
        assert create.await_args.kwargs == {"include_cost": True}
    assert ctx.counts["masters"] == {
        "created": int(existing_kind == "none"),
        "existing": int(existing_kind != "none"),
    }
