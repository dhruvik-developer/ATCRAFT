"""Keep route names, matching order and resource/job status codes consistent.

Routes come from :mod:`tests.route_support`, which walks FastAPI's lazy
included routers. Until 8 November 2026 this file built its own parent router
and kept only ``APIRoute`` entries - of which, under FastAPI 0.141, there are
none - so every test below passed over an empty list.
``test_the_guard_sees_every_documented_route`` exists so that cannot recur.
"""

from __future__ import annotations

import re

import pytest
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
