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
"""

from __future__ import annotations

from sqlalchemy import Column, Table

from src.core.models import Base
from src.core.registry import import_all_models
from src.core.types import GUID


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
