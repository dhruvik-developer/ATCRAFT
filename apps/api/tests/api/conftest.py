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

    ``create_app`` includes ``api_router`` at import time, but it is
    ``register_module_routers`` that puts the module routers *into* it, and that
    runs from the lifespan - which httpx's ASGI transport never executes. Without
    this, every request in ``tests/api`` answers 404 and a test "passes" by
    never reaching a handler at all.

    Autouse and session-scoped, here rather than in one test module, because the
    failure it prevents is silent: a new API test file gets 404s that look like
    a routing mistake in the code under test. Guarded on ``api_router.routes``
    so mounting twice - which would duplicate all 25 module routers in a
    process-global app the whole suite shares - cannot happen.
    """
    from src.api.router import api_router, register_module_routers

    if not api_router.routes:
        register_module_routers()
