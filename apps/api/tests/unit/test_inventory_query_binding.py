"""Inventory list filters answer 422, never 500, for a bad query string.

The list routes build their filter models inside a dependency function. FastAPI
turns a *parameter-binding* failure into the 422 envelope, but a pydantic error
raised from the dependency body itself is an unhandled exception: with the
window parameters typed ``str`` and the model built by hand,
``?date_from=31/12/2024`` answered 500 INTERNAL_ERROR and told the user to quote
a request id. These tests drive the real ASGI application with the database and
authentication dependencies overridden - no database - and assert the envelope
a client actually receives.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError as PydanticValidationError

import src.modules.inventory.router as inventory_router
from src.core.config import settings
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.exception_handlers import register_exception_handlers
from src.core.responses import CursorPagination
from src.modules.inventory.schemas import LedgerFilters

pytestmark = pytest.mark.unit

PREFIX = settings.api_v1_prefix


async def _no_database() -> AsyncIterator[None]:
    # Every request below is refused - or answered by a stub - before a session
    # would be used; yielding ``None`` proves nothing reaches the database.
    yield None


def _superuser() -> Principal:
    return Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="binding@example.test",
        full_name="Binding Test",
        is_superuser=True,
        permissions=frozenset(),
    )


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    # A private application holding only the inventory router and the real
    # exception handlers - the two things the envelope depends on. Mounting the
    # module routers into the process-global ``api_router`` instead would leak
    # into every later test that builds the full app, and the first request to
    # such an app costs seconds, which the readiness probe tests time out on.
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(inventory_router.router, prefix=PREFIX)
    application.dependency_overrides[get_db] = _no_database
    application.dependency_overrides[get_current_principal] = _superuser
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.mark.parametrize(
    ("path", "query", "field"),
    [
        ("/inventory/ledger", {"occurred_from": "31/12/2024"}, "query.occurred_from"),
        (
            "/inventory/ledger",
            {"occurred_from": "2026-02-01T00:00:00", "occurred_to": "2026-01-01T00:00:00"},
            "query.occurred_to",
        ),
        ("/inventory/transfers", {"date_from": "31/12/2024"}, "query.date_from"),
        ("/inventory/adjustments", {"date_to": "not-a-date"}, "query.date_to"),
        ("/inventory/physical-counts", {"date_from": "2024-13-45"}, "query.date_from"),
    ],
)
async def test_a_bad_filter_value_is_a_422_naming_the_field(
    client: httpx.AsyncClient, path: str, query: dict[str, str], field: str
) -> None:
    response = await client.get(f"{PREFIX}{path}", params=query)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert field in body["error"]["details"]["fields"]


async def test_a_well_formed_window_reaches_the_handler_parsed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative cases above would also pass if nothing ever bound.

    So the positive path is pinned too: a valid window arrives at the handler
    as datetimes, not strings, and the route answers normally.
    """
    seen: list[LedgerFilters] = []

    class _Recorder:
        def __init__(self, session: Any, tenant: Any) -> None:
            pass

        async def list_ledger(
            self, filters: LedgerFilters, params: Any
        ) -> tuple[list[Any], CursorPagination]:
            seen.append(filters)
            return [], CursorPagination()

    monkeypatch.setattr(inventory_router, "InventoryQueryService", _Recorder)

    response = await client.get(
        f"{PREFIX}/inventory/ledger",
        params={"occurred_from": "2026-01-01T00:00:00", "occurred_to": "2026-01-31T23:59:59"},
    )

    assert response.status_code == 200, response.text
    assert seen[0].occurred_from == datetime.fromisoformat("2026-01-01T00:00:00")
    assert seen[0].occurred_to == datetime.fromisoformat("2026-01-31T23:59:59")


class TestLedgerWindowRule:
    def test_an_inverted_window_is_reported_against_occurred_to(self) -> None:
        with pytest.raises(PydanticValidationError) as exc:
            LedgerFilters(
                occurred_from=datetime(2026, 2, 1, tzinfo=UTC),
                occurred_to=datetime(2026, 1, 1, tzinfo=UTC),
            )
        assert [error["loc"] for error in exc.value.errors()] == [("occurred_to",)]

    def test_an_ordered_window_is_accepted(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        filters = LedgerFilters(occurred_from=moment, occurred_to=moment)
        assert filters.occurred_from == filters.occurred_to

    def test_a_mixed_awareness_window_is_passed_through_not_crashed(self) -> None:
        # Python cannot compare a naive datetime with an aware one; the rule
        # must step aside rather than raise TypeError, which no handler maps.
        naive_upper = datetime.fromisoformat("2026-01-01T00:00:00")
        filters = LedgerFilters(
            occurred_from=datetime(2026, 1, 2, tzinfo=UTC), occurred_to=naive_upper
        )
        assert filters.occurred_to == naive_upper
