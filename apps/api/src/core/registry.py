"""Model registry - the single import point for SQLAlchemy metadata.

Alembic autogenerate only sees tables whose model modules have been imported.
Maintaining that list by hand is a reliable way to ship a migration that silently
omits a new table, so every ``src/modules/*/models.py`` is discovered and
imported instead.

Import both here and in ``migrations/env.py``::

    from src.core.registry import metadata, import_all_models
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from sqlalchemy import MetaData

from src.core.models import Base

MODULES_PACKAGE = "src.modules"
_MODULES_PATH = Path(__file__).resolve().parent.parent / "modules"

_imported = False


def import_all_models() -> list[str]:
    """Import every module's models so they register on ``Base.metadata``.

    Idempotent, and safe to call at application startup as well as from Alembic.
    Returns the module names that were imported, which the tests assert against
    so a module that stops registering its models fails loudly.
    """
    global _imported
    imported: list[str] = []

    for entry in sorted(pkgutil.iter_modules([str(_MODULES_PATH)])):
        if not entry.ispkg:
            continue
        module_name = f"{MODULES_PACKAGE}.{entry.name}.models"
        try:
            importlib.import_module(module_name)
            imported.append(module_name)
        except ModuleNotFoundError as exc:
            # A module package without a models.py is legitimate: some modules
            # (reports, notifications) are pure read/dispatch layers. Only
            # swallow the "no such module" case - a genuine import error inside
            # a models file must still surface.
            if exc.name not in (module_name, f"{MODULES_PACKAGE}.{entry.name}.models"):
                raise

    # Tables defined outside a module package.
    importlib.import_module("src.common.codes")
    imported.append("src.common.codes")

    _imported = True
    return imported


def get_metadata() -> MetaData:
    """Fully populated metadata for Alembic and for test schema creation."""
    if not _imported:
        import_all_models()
    return Base.metadata


metadata: MetaData = Base.metadata

__all__ = ["get_metadata", "import_all_models", "metadata"]
