"""Every route answers in the shared envelope for requests any client can make.

Feature tests call the handful of routes their feature needs; until November
2026 the bodies of 554 of the 785 routes were never executed by any backend
test. This file asks two questions of *every* route instead:

* A read with no path parameters - a list, a register, a report, a summary -
  answers a fully authorised user of a brand-new company with a success
  envelope, or with a 4xx error envelope when it needs a parameter the client
  did not send. Never a 500: an empty company is the first thing every
  customer's tenant is, and the case the list-endpoint ``lazy="raise"`` defect
  hid behind.
* A route that addresses one resource by id answers 404 in the error envelope
  when no such resource exists in the caller's company. Never a 200 that
  reports on nothing, and never a 500.

Routes that need a request body are skipped by the second question: body
validation answers first (422), which ``test_api_response_contract.py`` already
pins. Portal routes refuse a staff principal before their bodies run; their
behaviour is covered by the vendor and dealer portal HTTP suites.
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from httpx import AsyncClient, Response

from src.core.clock import utcnow
from src.core.errors import ErrorCode
from tests.route_support import MountedRoute, module_routes

pytestmark = [pytest.mark.api, pytest.mark.integration]

#: A single request may not take longer than this; a hang is a failure, not a wait.
REQUEST_DEADLINE_SECONDS = 30

#: Routes that legitimately answer something other than 404 for an id that
#: does not exist, with the reason. Keep this short: each entry is a claim
#: that "unknown id" is not an error for that route.
#: Nested collection reads return an empty page when the parent is missing
#: rather than probing the parent first - the empty list is the contract.
NOT_FOUND_EXEMPT: dict[tuple[str, str], str] = {
    ("GET", "/warehouses/{warehouse_id}/locations"): "empty locations for an unknown warehouse",
    ("GET", "/vendors/{vendor_id}/contacts"): "empty contacts for an unknown vendor",
    ("GET", "/vendors/{vendor_id}/categories"): "empty categories for an unknown vendor",
    ("GET", "/bom/where-used/{item_id}"): "empty where-used for an unknown item",
    ("GET", "/bom/items/{item_id}/versions"): "empty BOM versions for an unknown item",
    ("GET", "/routing/items/{item_id}/versions"): "empty routing versions for an unknown item",
    ("GET", "/routing/items/{item_id}/lead-time"): "zero lead time for an unknown item",
    ("GET", "/mrp/sales-orders/{order_id}/expected-availability"): (
        "empty availability for an unknown order"
    ),
    ("GET", "/mrp/runs/{run_id}/suggestions"): "empty suggestions for an unknown run",
    ("GET", "/production/{order_id}/entries"): "empty entries for an unknown order",
    ("GET", "/production/{order_id}/work-orders"): "empty work orders for an unknown order",
    ("GET", "/quality/reports/item/{item_id}"): "zeroed quality report for an unknown item",
    ("GET", "/quality/reports/vendor/{vendor_id}"): "zeroed quality report for an unknown vendor",
    ("GET", "/packing/trace/serial/{serial_id}"): "empty packing trace for an unknown serial",
    ("GET", "/audit/entity/{entity_type}/{entity_id}"): "empty audit trail for an unknown entity",
}


def _walk(dependant: Dependant) -> list[Dependant]:
    found, pending = [], [dependant]
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(current.dependencies)
    return found


def _sample(annotation: Any) -> str | None:
    """A syntactically valid value for a parameter of this type, or None."""
    if isinstance(annotation, type):
        if issubclass(annotation, uuid.UUID):
            return str(uuid.uuid4())
        if issubclass(annotation, enum.Enum):
            return str(next(iter(annotation)).value)
        if issubclass(annotation, bool):
            return "false"
        if issubclass(annotation, int):
            return "1"
        if issubclass(annotation, Decimal):
            return "1"
        if issubclass(annotation, date):
            return utcnow().date().isoformat()
        if issubclass(annotation, str):
            return "sample"
    return None


def _required_query(mounted: MountedRoute) -> dict[str, str] | None:
    params: dict[str, str] = {}
    for dependant in _walk(mounted.route.dependant):
        for field in dependant.query_params:
            if not field.field_info.is_required():
                continue
            value = _sample(field.field_info.annotation)
            if value is None:
                return None
            params[field.alias] = value
    return params


def _path_annotations(mounted: MountedRoute) -> dict[str, Any]:
    annotations: dict[str, Any] = {}
    for dependant in _walk(mounted.route.dependant):
        for field in dependant.path_params:
            annotations[field.alias] = field.field_info.annotation
    return annotations


def _needs_body(mounted: MountedRoute) -> bool:
    return any(
        field.field_info.is_required()
        for dependant in _walk(mounted.route.dependant)
        for field in dependant.body_params
    )


def _is_portal(mounted: MountedRoute) -> bool:
    return mounted.module in {"vendor_portal", "dealer_portal"}


def _collection_reads() -> list[Any]:
    cases = []
    for mounted in module_routes():
        if "GET" not in mounted.methods or "{" in mounted.path or _is_portal(mounted):
            continue
        query = _required_query(mounted)
        if query is None:
            continue
        cases.append(pytest.param(mounted, query, id=mounted.path))
    return cases


def _by_id_routes() -> list[Any]:
    cases = []
    for mounted in module_routes():
        if "{" not in mounted.path or _is_portal(mounted) or _needs_body(mounted):
            continue
        annotations = _path_annotations(mounted)
        if not any(isinstance(a, type) and issubclass(a, uuid.UUID) for a in annotations.values()):
            continue
        path, complete = mounted.path, True
        for name, annotation in annotations.items():
            value = _sample(annotation)
            if value is None:
                complete = False
                break
            path = path.replace("{" + name + "}", value)
        query = _required_query(mounted)
        if not complete or query is None:
            continue
        for method in sorted(mounted.methods):
            cases.append(pytest.param(method, mounted, path, query, id=f"{method} {mounted.path}"))
    return cases


async def _send(client: AsyncClient, method: str, path: str, query: dict[str, str]) -> Response:
    return await asyncio.wait_for(
        client.request(method, path, params=query), timeout=REQUEST_DEADLINE_SECONDS
    )


def _assert_safe(response: Response) -> None:
    text = response.text
    assert "Traceback" not in text and "sqlalchemy" not in text.lower(), text[:300]


@pytest.mark.parametrize(("mounted", "query"), _collection_reads())
async def test_a_collection_read_answers_an_empty_company_in_the_envelope(
    mounted: MountedRoute, query: dict[str, str], api_client: AsyncClient
) -> None:
    response = await _send(api_client, "GET", mounted.path, query)
    _assert_safe(response)
    assert response.status_code < 500, (
        f"{mounted} answered {response.status_code}: {response.text[:300]}"
    )
    if not response.headers.get("content-type", "").startswith("application/json"):
        # A file download keeps its binary contract; it only has to succeed.
        assert response.status_code == 200, response.status_code
        return
    body = response.json()
    if response.status_code == 200:
        assert body["success"] is True and "data" in body, body
    else:
        assert body["success"] is False and body["error"]["code"], body


@pytest.mark.parametrize(("method", "mounted", "path", "query"), _by_id_routes())
async def test_an_unknown_id_is_a_404_in_the_error_envelope(
    method: str,
    mounted: MountedRoute,
    path: str,
    query: dict[str, str],
    api_client: AsyncClient,
) -> None:
    response = await _send(api_client, method, path, query)
    _assert_safe(response)
    if (method, mounted.path) in NOT_FOUND_EXEMPT:
        assert response.status_code < 500, response.text[:300]
        return
    assert response.status_code == 404, (
        f"{method} {mounted.path} answered {response.status_code}: {response.text[:300]}"
    )
    body = response.json()
    assert body["success"] is False
    # NOT_FOUND, or one of the documented specific codes (JOB_NOT_FOUND,
    # BOM_NOT_FOUND, FILE_NOT_FOUND ...) that docs/api.md lists per area.
    code = body["error"]["code"]
    assert code == ErrorCode.NOT_FOUND or (
        code.endswith("_NOT_FOUND") and code in ErrorCode.__members__
    ), body["error"]
