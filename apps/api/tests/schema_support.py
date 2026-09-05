"""Metadata preparation shared by every database-backed test module.

Modules land in dependency waves, so at any moment some foreign key points at a
table whose module does not exist yet (``users.vendor_id`` -> ``vendors.id``).
SQLAlchemy resolves foreign keys against the metadata rather than against the
database, so ``create_all`` fails outright on a dangling target - and every test
module that builds its own private schema hit that wall and grew its own copy of
the workaround.

The copies were the real defect. A hand-written stand-in states a row shape the
owning module has not defined yet, so it goes stale the moment that module lands
and the test starts failing for a reason that has nothing to do with what it
asserts. Worse, ``Table(name, Base.metadata, ...)`` raises on a name that is
already registered, so two files disagreeing about which tables are missing is a
collision waiting for an import order to expose it.

One implementation lives here. It names no tables, so nothing in it can go stale,
and it is idempotent, so any number of modules may call it.

The same file owns :func:`report_database_setup_failure`, the one place that
decides what a database set-up error means for the run - so a module fixture
that builds its own schema cannot quietly turn a broken model into a skip.
"""

from __future__ import annotations

import os
from typing import NoReturn

import asyncpg
import pytest
from sqlalchemy import Column, Table

from src.core.models import Base
from src.core.registry import import_all_models
from src.core.types import GUID

#: asyncpg's own "the server is not taking connections" family. Anything else
#: it raises - a refused CREATE DATABASE, a missing extension, a broken model
#: failing ``create_all`` - is a real failure and must surface as one.
_ASYNCPG_UNREACHABLE = (
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.PostgresConnectionError,
)


def database_unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` means PostgreSQL could not be reached at all.

    A refused port arrives as ``ConnectionRefusedError`` and an unknown host as
    ``socket.gaierror`` - both ``OSError`` - and SQLAlchemy wraps asyncpg's
    start-up refusals in a ``DBAPIError`` whose cause chain ends at the asyncpg
    exception. The chain is walked so the wrapping does not hide the cause.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError | _ASYNCPG_UNREACHABLE):
            return True
        current = current.__cause__ or current.__context__
    return False


def report_database_setup_failure(
    exc: BaseException, *, purpose: str = "integration tests"
) -> NoReturn:
    """Turn a database set-up error into the right pytest outcome, and only that.

    Three outcomes, chosen by what went wrong and where:

    * The server is unreachable on a developer machine: skip, with the command
      that starts it. Nothing about the code is known to be wrong.
    * The server is unreachable under CI (``CI`` is set): fail. A pipeline that
      never brought PostgreSQL up must not report a green run that executed
      nothing.
    * Anything else - the database could not be created, an extension is
      missing, ``create_all`` rejected a model: re-raise. These are defects in
      the schema or the environment contract, and a blanket skip here once hid
      all of them behind "PostgreSQL is not reachable".
    """
    if not database_unreachable(exc):
        raise exc
    message = (
        f"PostgreSQL is not reachable for {purpose} ({exc!r}). "
        "Start it with `make dev` before running database-backed tests."
    )
    if os.environ.get("CI"):
        pytest.fail(message, pytrace=False)
    pytest.skip(message, allow_module_level=True)


def register_missing_fk_targets() -> set[str]:
    """Register a one-column stand-in for every dangling foreign-key target.

    A placeholder carries ``id`` and nothing else, which is all a foreign key
    needs to resolve. It disappears on its own the moment the real module lands,
    because :func:`import_all_models` then registers the genuine table first and
    the name is no longer missing.

    Idempotent: a name already present in the metadata is never re-registered,
    so repeated calls - and calls from several test modules - are safe.
    """
    missing: set[str] = set()
    for table in list(Base.metadata.tables.values()):
        for foreign_key in table.foreign_keys:
            target = foreign_key.target_fullname.split(".")[0]
            if target not in Base.metadata.tables:
                missing.add(target)

    for name in sorted(missing):
        Table(name, Base.metadata, Column("id", GUID, primary_key=True))
    return missing


def prepare_metadata() -> set[str]:
    """Make ``Base.metadata`` creatable, and return the placeholder names.

    Call this before any ``Base.metadata.create_all``. It imports every module's
    models - so a table is only ever stood in for while its module genuinely
    does not exist - and then fills whatever the imports still leave dangling.
    """
    import_all_models()
    return register_missing_fk_targets()
