"""Keep route names, matching order and resource/job status codes consistent."""

from __future__ import annotations

import importlib
import re

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

from src.api.router import MODULE_ORDER
from src.core.responses import AcceptedJobResponse

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def routes() -> list[APIRoute]:
    router = APIRouter()
    for name in MODULE_ORDER:
        module = importlib.import_module(f"src.modules.{name}.router")
        router.include_router(module.router)
    return [route for route in router.routes if isinstance(route, APIRoute)]


def test_public_path_and_parameter_names_follow_the_shared_convention(
    routes: list[APIRoute],
) -> None:
    for route in routes:
        for segment in route.path.split("/"):
            if not segment:
                continue
            if segment.startswith("{"):
                assert re.fullmatch(r"\{[a-z][a-z0-9]*(?:_[a-z0-9]+)*\}", segment), route.path
            else:
                assert re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", segment), route.path
        pending = [route.dependant]
        while pending:
            dependency = pending.pop()
            for parameter in dependency.query_params:
                assert re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", parameter.alias), (
                    route.path,
                    parameter.alias,
                )
            pending.extend(dependency.dependencies)


def test_routes_do_not_duplicate_or_shadow_a_later_route(routes: list[APIRoute]) -> None:
    for index, route in enumerate(routes):
        for previous in routes[:index]:
            if route.methods & previous.methods:
                assert not previous.path_regex.fullmatch(route.path), (
                    f"{previous.path} intercepts {route.path} "
                    f"for {route.methods & previous.methods}"
                )


def test_resource_creation_and_queued_jobs_document_their_http_status(
    routes: list[APIRoute],
) -> None:
    for route in routes:
        if "POST" in route.methods and route.name.startswith(("create_", "bulk_create_")):
            assert route.status_code == 201, route.path
        if route.response_model is AcceptedJobResponse:
            assert route.status_code == 202, route.path
