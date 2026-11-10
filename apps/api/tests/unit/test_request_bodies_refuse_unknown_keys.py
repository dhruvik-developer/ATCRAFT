"""A request body refuses a key it does not define, at every depth.

238 request bodies answered an unknown key with a 422 that names it, while 77
models dropped one silently - so a misspelt optional field on ``POST /customers``
was saved as its default and answered 201, while the same typo on
``PATCH /customers/{id}`` was a 422: two contracts, split inside a module. Seven
more dropped a key inside a line (an adjustment's ``unit_cst``). Every request
model now forbids extras (``model_config = ConfigDict(extra="forbid")``).

Two guards hold it. The structural one reads every route's body model - an
optional one, and every model nested in one, included - and names any that
would drop a key. The HTTP one sends an unknown key to every JSON body route
through the real application, with a session that refuses to be used, and
proves the 422 names the key before anything is read.
"""

from __future__ import annotations

import typing
from collections.abc import AsyncIterator, Iterable
from typing import Annotated

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict, Field

from src.core.config import settings
from src.core.errors import ErrorCode
from tests.route_support import (
    PORTAL_PREFIXES,
    MountedRoute,
    module_routes,
    mounted_application,
    route_url,
    superuser_without_database,
)

pytestmark = pytest.mark.unit

#: Bodies that must accept keys they do not define, and why - a third party's
#: payload whose shape is theirs to extend. Keyed "<module>.<Model>".
LENIENT_ON_PURPOSE: dict[str, str] = {}

#: A key no request model defines.
UNKNOWN_KEY = "not_a_field_of_this_body"


def body_models(mounted: MountedRoute) -> list[type[BaseModel]]:
    """The request body models of a route, unwrapping an optional body."""
    models: list[type[BaseModel]] = []
    pending = [mounted.route.dependant]
    while pending:
        dependant = pending.pop()
        for field in dependant.body_params:
            annotation = field.field_info.annotation
            candidates = typing.get_args(annotation) or (annotation,)
            models.extend(
                candidate
                for candidate in candidates
                if isinstance(candidate, type) and issubclass(candidate, BaseModel)
            )
        pending.extend(dependant.dependencies)
    return models


def request_models(mounted: MountedRoute) -> set[type[BaseModel]]:
    """Every model a route's body is validated against: its own and each one nested in it."""
    found: set[type[BaseModel]] = set()
    pending: list[object] = list(body_models(mounted))
    while pending:
        annotation = pending.pop()
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation not in found:
                found.add(annotation)
                pending.extend(field.annotation for field in annotation.model_fields.values())
        else:
            # list[Line], Line | None, Annotated[list[Line], ...], dict[str, Line]
            pending.extend(typing.get_args(annotation))
    return found


def lenient_bodies(routes: Iterable[MountedRoute]) -> set[str]:
    """``<module>.<Model>`` of every request model, nested ones included, that keeps extras."""
    return {
        f"{model.__module__.removeprefix('src.modules.')}.{model.__name__}"
        for mounted in routes
        for model in request_models(mounted)
        if model.model_config.get("extra") != "forbid"
    }


def test_every_request_body_refuses_an_unknown_key() -> None:
    lenient = lenient_bodies(module_routes())
    assert sorted(lenient - set(LENIENT_ON_PURPOSE)) == [], (
        "Give the request model model_config = ConfigDict(extra='forbid'), "
        "or list it in LENIENT_ON_PURPOSE with its reason."
    )
    assert sorted(set(LENIENT_ON_PURPOSE) - lenient) == [], (
        "Strict now, or no longer a request body: remove it from LENIENT_ON_PURPOSE."
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class _Lenient(BaseModel):
    name: str


class _StrictWithLenientLines(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: Annotated[list[_Lenient], Field(max_length=5)] | None = None


def test_the_rule_tells_a_lenient_body_from_a_strict_one() -> None:
    probe = APIRouter()

    @probe.post("/strict")
    async def strict(payload: _Strict) -> None: ...

    @probe.post("/lenient")
    async def lenient(payload: _Lenient) -> None: ...

    @probe.post("/optional-lenient")
    async def optional_lenient(payload: _Lenient | None = None) -> None: ...

    @probe.post("/lenient-lines")
    async def lenient_lines(payload: _StrictWithLenientLines) -> None: ...

    mounted = [
        MountedRoute(module="probe", path=route.path, route=route)
        for route in probe.routes
        if isinstance(route, APIRoute)
    ]
    assert lenient_bodies(mounted[:1]) == set()
    assert lenient_bodies(mounted[1:2]) == {f"{__name__}._Lenient"}
    assert lenient_bodies(mounted[2:3]) == {f"{__name__}._Lenient"}
    assert lenient_bodies(mounted[3:]) == {f"{__name__}._Lenient"}


def test_a_screen_that_saves_the_rows_it_read_sends_only_keys_the_update_defines() -> None:
    # The messaging screen PUTs back the template rows GET handed it, edited in
    # place, so a field added to the read model alone would turn every save
    # into a 422 naming it. Add it to the update model, or strip it on the screen.
    from src.modules.notifications.schemas import (
        NotificationChannelTemplateRead,
        NotificationChannelTemplateUpdate,
    )

    read = set(NotificationChannelTemplateRead.model_fields)
    assert read <= set(NotificationChannelTemplateUpdate.model_fields), sorted(
        read - set(NotificationChannelTemplateUpdate.model_fields)
    )


# -----------------------------------------------------------------------------
# HTTP: every JSON body route, through the real application
# -----------------------------------------------------------------------------


def _body_operations() -> list[tuple[str, MountedRoute]]:
    return [
        (method, mounted)
        for mounted in module_routes()
        if body_models(mounted) and not mounted.path.startswith(PORTAL_PREFIXES)
        for method in sorted(mounted.methods)
    ]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    async with superuser_without_database(mounted_application()) as pair:
        yield pair


def test_the_http_guard_sees_the_bodies_that_once_dropped_a_key() -> None:
    operations = {(method, mounted.path) for method, mounted in _body_operations()}
    assert {
        ("POST", "/auth/login"),
        ("POST", "/customers"),
        ("POST", "/products/items"),
        ("PATCH", "/products/items/{item_id}"),
        ("PUT", "/notifications/preferences"),
    } <= operations


@pytest.mark.parametrize(
    ("method", "mounted"),
    [
        pytest.param(method, mounted, id=f"{method} {mounted.path}")
        for method, mounted in _body_operations()
    ],
)
async def test_an_unknown_key_is_refused_by_name_before_anything_is_read(
    method: str, mounted: MountedRoute, client: tuple[AsyncClient, list[str]]
) -> None:
    http, touched = client
    response = await http.request(
        method, settings.api_v1_prefix + route_url(mounted), json={UNKNOWN_KEY: 1}
    )

    assert not touched, f"the database was used before the body was validated: {touched}"
    assert response.status_code == 422, response.text[:300]
    error = response.json()["error"]
    assert error["code"] == ErrorCode.VALIDATION_ERROR
    assert UNKNOWN_KEY in error["details"]["fields"], error["details"]["fields"]
