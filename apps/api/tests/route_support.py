"""Enumerate the API's routes the way the application mounts and matches them.

FastAPI 0.141 no longer copies an included router's routes into the including
router. ``include_router`` appends one lazy ``_IncludedRouter`` entry that keeps
a reference to the original router and the prefix it was included under. So
``[r for r in router.routes if isinstance(r, APIRoute)]`` over a router that
*includes* others returns an empty list, and a guard iterating it passes having
checked nothing - which is exactly what ``test_http_route_conventions.py`` did
from the day it was written until 8 November 2026: three conventions, zero
routes.

This module walks the inclusions and carries their prefixes, and it refuses to
guess: an entry that is neither a route nor an included router raises, so the
next change of FastAPI internals fails loudly instead of emptying every guard
built on top of it. :func:`assert_matches_contract` is the second line of
defence - the routes found here must be exactly the operations in the
committed ``openapi.json``, which is generated from the running application.
"""

from __future__ import annotations

import enum
import importlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from src.api.router import MODULE_ORDER
from src.core.config import settings
from src.core.database import get_db
from src.core.deps import Principal, get_current_principal
from src.core.errors import AppError, ErrorCode

#: The committed contract, generated from the application by
#: ``scripts.export_openapi`` and kept current by ``make contract-check``.
OPENAPI_PATH = Path(__file__).resolve().parents[3] / "openapi.json"

_ROUTE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class MountedRoute:
    """One API route with the full path it is served at below the API prefix."""

    module: str
    path: str
    route: APIRoute

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self.route.methods or ()) & _ROUTE_METHODS

    @property
    def operations(self) -> list[tuple[str, str]]:
        return [(method, self.path) for method in sorted(self.methods)]

    def __str__(self) -> str:
        return f"{','.join(sorted(self.methods))} {self.path}"


def iter_api_routes(router: APIRouter, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Yield ``(full_path, route)`` for every API route under ``router``, in match order."""
    for entry in router.routes:
        if isinstance(entry, APIRoute):
            yield prefix + entry.path, entry
            continue
        original = getattr(entry, "original_router", None)
        if not isinstance(original, APIRouter):
            raise TypeError(
                f"Unrecognised entry {type(entry).__name__!r} under prefix {prefix!r}. "
                "FastAPI's router internals changed; teach iter_api_routes about it "
                "rather than skipping it, or every route guard silently checks less."
            )
        context = getattr(entry, "include_context", None)
        yield from iter_api_routes(original, prefix + (getattr(context, "prefix", "") or ""))


@cache
def module_routes() -> tuple[MountedRoute, ...]:
    """Every module route, in the order ``register_module_routers`` mounts them."""
    found: list[MountedRoute] = []
    for name in MODULE_ORDER:
        module = importlib.import_module(f"src.modules.{name}.router")
        found.extend(
            MountedRoute(module=name, path=path, route=route)
            for path, route in iter_api_routes(module.router)
        )
    return tuple(found)


def contract_operations() -> set[tuple[str, str]]:
    """``(METHOD, path)`` for every operation in the committed OpenAPI contract."""
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    prefix = settings.api_v1_prefix
    operations: set[tuple[str, str]] = set()
    for path, item in spec["paths"].items():
        relative = path.removeprefix(prefix) if path.startswith(prefix) else path
        operations.update(
            (method.upper(), relative) for method in item if method.upper() in _ROUTE_METHODS
        )
    return operations


def assert_matches_contract(routes: tuple[MountedRoute, ...]) -> None:
    """Fail unless ``routes`` are exactly the operations the contract documents.

    Routes hidden from the schema (``include_in_schema=False``) are excluded on
    the route side, so the comparison is like for like.
    """
    found = {
        operation
        for mounted in routes
        if mounted.route.include_in_schema
        for operation in mounted.operations
    }
    documented = contract_operations()
    missing, extra = sorted(documented - found), sorted(found - documented)
    assert not missing and not extra, (
        "Route enumeration disagrees with openapi.json - a guard built on it would "
        f"check the wrong set. In the contract but not found: {missing[:10]}; "
        f"found but not in the contract: {extra[:10]}"
    )


# -----------------------------------------------------------------------------
# Driving a route over HTTP without a database
# -----------------------------------------------------------------------------


class DatabaseReachedError(AppError):
    """The stand-in session's answer: the request reached for the database.

    An ``AppError`` so the application answers it like any refusal (503). An
    unhandled exception would be logged with its traceback and locals, which
    the development renderer takes most of a minute to draw.
    """

    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    message = "The request reached the database."


class UntouchableSession:
    """Stands in for the request session: records every use of it, and refuses it.

    Override ``get_db`` with it to prove a request is decided - refused, or
    answered - before anything is read or written.
    """

    def __init__(self, touched: list[str]) -> None:
        object.__setattr__(self, "_touched", touched)

    def __getattr__(self, name: str) -> Any:
        self._touched.append(name)
        raise DatabaseReachedError()


def _sample(annotation: Any) -> str:
    if isinstance(annotation, type):
        if issubclass(annotation, uuid.UUID):
            return str(uuid.uuid4())
        if issubclass(annotation, enum.Enum):
            return str(next(iter(annotation)).value)
        if issubclass(annotation, bool):
            return "false"
        if issubclass(annotation, int):
            return "1"
        if issubclass(annotation, date):
            return "2026-04-01"
    return "sample"


def route_url(mounted: MountedRoute) -> str:
    """The route's path below the API prefix, each path parameter a valid sample."""
    annotations: dict[str, Any] = {}
    pending = [mounted.route.dependant]
    while pending:
        dependant = pending.pop()
        for field in dependant.path_params:
            annotations[field.alias] = field.field_info.annotation
        pending.extend(dependant.dependencies)
    path = mounted.path
    for name, annotation in annotations.items():
        path = path.replace("{" + name + "}", _sample(annotation))
    assert "{" not in path, f"unfilled path parameter in {mounted.path}"
    return path


#: A portal account's routes answer a staff principal with SCOPE_DENIED before
#: the body is read; ``tests/api`` drives them with a portal principal.
PORTAL_PREFIXES = ("/dealer-portal/", "/vendor-portal/")


def mounted_application() -> FastAPI:
    """The application with every module's router mounted, as a request meets it."""
    from src.api.router import api_router, register_module_routers
    from src.main import app

    # Same guard as tests/api/conftest.py: the process-global router must be
    # filled once, never twice.
    if not api_router.routes:
        register_module_routers()
    return app


@asynccontextmanager
async def superuser_without_database(
    app: FastAPI,
) -> AsyncIterator[tuple[AsyncClient, list[str]]]:
    """A client signed in as a superuser whose request session refuses every use.

    Yields the client and the names of the session attributes requests reached
    for: an empty list proves a request was decided before anything was read.
    """
    touched: list[str] = []
    administrator = Principal(
        user_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        email="route.guard@example.com",
        full_name="Route Guard",
        is_superuser=True,
        permissions=frozenset(),
        role_codes=(),
    )

    async def _session() -> AsyncIterator[UntouchableSession]:
        yield UntouchableSession(touched)

    app.dependency_overrides[get_db] = _session
    app.dependency_overrides[get_current_principal] = lambda: administrator
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as http:
            yield http, touched
    finally:
        app.dependency_overrides.clear()
