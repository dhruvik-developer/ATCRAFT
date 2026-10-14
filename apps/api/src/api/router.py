"""Aggregate API router for ``/api/v1``.

Each module owns a ``router.py`` exposing an ``APIRouter`` named ``router`` with
its own prefix and tags. This file only decides which modules are mounted and in
what order, so the OpenAPI document groups sensibly (master rules section 68)
and adding a module is a one-line change.
"""

from __future__ import annotations

import importlib

from fastapi import APIRouter

from src.core.logging import get_logger

log = get_logger(__name__)

api_router = APIRouter()

#: Mount order drives the order of tag groups in Swagger. Roughly: identity,
#: masters, then the operational flow from demand through to dispatch.
MODULE_ORDER: tuple[str, ...] = (
    "auth",
    "users",
    "organization",
    "warehouses",
    "products",
    "customers",
    "vendors",
    "inventory",
    "bom",
    "routing",
    "sales",
    "allocation",
    "mrp",
    "procurement",
    "production",
    "assembly",
    "job_work",
    "quality",
    "packing",
    "dispatch",
    "finance",
    "reports",
    "documents",
    "notifications",
    "vendor_portal",
    "dealer_portal",
    "audit",
)


def register_module_routers() -> list[str]:
    """Include every module router that exists. Returns the mounted module names.

    A module without a ``router.py`` is skipped: not every module has an HTTP
    surface of its own. Any *other* import failure propagates - a module that
    exists but is broken must fail startup loudly rather than silently vanish
    from the API.
    """
    mounted: list[str] = []

    for name in MODULE_ORDER:
        module_path = f"src.modules.{name}.router"
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            if exc.name == module_path:
                continue
            raise

        module_router = getattr(module, "router", None)
        if module_router is None:
            log.warning("router.missing_attribute", module=module_path)
            continue

        api_router.include_router(module_router)
        mounted.append(name)

    log.info("router.mounted", modules=mounted, count=len(mounted))
    return mounted


__all__ = ["MODULE_ORDER", "api_router", "register_module_routers"]
