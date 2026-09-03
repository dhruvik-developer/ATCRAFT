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
