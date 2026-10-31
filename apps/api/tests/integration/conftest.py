"""Database-backed tests.

The autouse fixture here is what makes ``tests/integration`` and ``tests/api``
require PostgreSQL while ``tests/unit`` stays a pure, fast suite that runs with
nothing installed but Python.

It also creates the test database itself, because several module test suites
build their own engine against ``settings.database_url`` rather than using the
shared ``session`` fixture — they need the database to exist before they connect.
"""

import pytest


@pytest.fixture(scope="session", autouse=True)
def _require_database(_database: None) -> None:
    """Ensure the test database and schema exist for every test in this tree."""


@pytest.fixture(scope="session", autouse=True)
def _mount_module_routers() -> None:
    """Fill in the module routers the way application startup does.

    ``create_app`` includes ``api_router`` at import time, but
    ``register_module_routers`` is what puts the module routers *into* it, and
    that runs from the lifespan - which httpx's ASGI transport never executes.
    Without this, an HTTP test in this tree answers 404 before it ever reaches a
    handler. Guarded on ``api_router.routes`` so a combined run with ``tests/api``
    still mounts each module router once.
    """
    from src.api.router import api_router, register_module_routers

    if not api_router.routes:
        register_module_routers()
