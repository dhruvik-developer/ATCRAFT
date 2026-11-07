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

import importlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.routing import APIRoute

from src.api.router import MODULE_ORDER
from src.core.config import settings

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
