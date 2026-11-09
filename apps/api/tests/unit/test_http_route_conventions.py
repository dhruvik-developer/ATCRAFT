"""Keep route names, matching order and resource/job status codes consistent.

Routes come from :mod:`tests.route_support`, which walks FastAPI's lazy
included routers. Until 8 November 2026 this file built its own parent router
and kept only ``APIRoute`` entries - of which, under FastAPI 0.141, there are
none - so every test below passed over an empty list.
``test_the_guard_sees_every_documented_route`` exists so that cannot recur.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.routing import compile_path

from src.core.responses import AcceptedJobResponse
from tests.route_support import MountedRoute, assert_matches_contract, module_routes

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def routes() -> tuple[MountedRoute, ...]:
    return module_routes()


def test_the_guard_sees_every_documented_route(routes: tuple[MountedRoute, ...]) -> None:
    assert_matches_contract(routes)


def test_public_path_and_parameter_names_follow_the_shared_convention(
    routes: tuple[MountedRoute, ...],
) -> None:
    for mounted in routes:
        for segment in mounted.path.split("/"):
            if not segment:
                continue
            if segment.startswith("{"):
                assert re.fullmatch(r"\{[a-z][a-z0-9]*(?:_[a-z0-9]+)*\}", segment), mounted.path
            else:
                assert re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", segment), mounted.path
        pending = [mounted.route.dependant]
        while pending:
            dependency = pending.pop()
            for parameter in dependency.query_params:
                assert re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", parameter.alias), (
                    mounted.path,
                    parameter.alias,
                )
            pending.extend(dependency.dependencies)


def test_routes_do_not_duplicate_or_shadow_a_later_route(
    routes: tuple[MountedRoute, ...],
) -> None:
    # Compare full paths: a nested router's own ``path_regex`` omits the prefix
    # it was included under, so matching on it would compare unlike things.
    compiled = [(mounted, compile_path(mounted.path)[0]) for mounted in routes]
    for index, (mounted, _) in enumerate(compiled):
        for previous, previous_regex in compiled[:index]:
            if mounted.methods & previous.methods:
                assert not previous_regex.fullmatch(mounted.path), (
                    f"{previous.path} intercepts {mounted.path} "
                    f"for {sorted(mounted.methods & previous.methods)}"
                )


def test_resource_creation_and_queued_jobs_document_their_http_status(
    routes: tuple[MountedRoute, ...],
) -> None:
    for mounted in routes:
        route = mounted.route
        if "POST" in route.methods and route.name.startswith(("create_", "bulk_create_")):
            assert route.status_code == 201, mounted.path
        if route.response_model is AcceptedJobResponse:
            assert route.status_code == 202, mounted.path


def summary_faults(routes: Iterable[MountedRoute]) -> list[str]:
    """Documented operations with no summary of their own, or one another shares.

    FastAPI titles a route that names no summary after its function, so
    ``list_credit_notes`` in finance and in the dealer portal both read "List
    Credit Notes" in Swagger and in the Postman collection - two requests nobody
    can tell apart. docs/api.md promises every operation a summary; this holds it
    to one that is its own. Compared without case, as a reader compares them.
    """
    faults: list[str] = []
    seen: dict[str, str] = {}
    for mounted in routes:
        if not mounted.route.include_in_schema:
            continue
        summary = (mounted.route.summary or "").strip()
        if not summary:
            faults.append(f"{mounted}: no summary")
            continue
        key = summary.casefold()
        if key in seen:
            faults.append(f"{mounted}: summary {summary!r} also names {seen[key]}")
        else:
            seen[key] = str(mounted)
    return faults


def test_every_documented_operation_has_a_summary_of_its_own(
    routes: tuple[MountedRoute, ...],
) -> None:
    assert summary_faults(routes) == []


def test_the_summary_rule_finds_a_missing_and_a_shared_summary() -> None:
    probe = APIRouter()

    @probe.get("/first", summary="List things")
    async def first() -> None: ...

    @probe.get("/second", summary="list things")
    async def second() -> None: ...

    @probe.get("/third")
    async def third() -> None: ...

    @probe.get("/hidden", include_in_schema=False)
    async def hidden() -> None: ...

    mounted = [
        MountedRoute(module="probe", path=route.path, route=route)
        for route in probe.routes
        if isinstance(route, APIRoute)
    ]
    assert summary_faults(mounted) == [
        "GET /second: summary 'list things' also names GET /first",
        "GET /third: no summary",
    ]
