"""The module and table maps name every module and table the code has.

docs/database.md section 2 is binding - it exists "so modules built in parallel
agree on table names" - and docs/architecture.md maps ``src/core`` and the
direction modules may depend in. Both drifted without a sound: section 2 had no
row for ``courier_tracking`` or ``auth`` and left out five live tables
(``courier_accounts``, ``courier_tracking_events``,
``credit_note_cost_allocations``, ``stock_batch_conversions``,
``stock_serial_conversions``); the Core table left out thirteen core modules;
and the dependency diagram placed neither ``courier_tracking`` nor ``auth``.
A map is prose, so nothing failed.

The rules:

* every table on ``Base.metadata`` is named in the section 2 row of the module
  that defines it - ``src/modules/<name>`` or ``src/common`` - and every table a
  row names is one a model defines;
* every package under ``src/modules`` has a row, and every row is such a
  package or ``common``;
* every module and package under ``src/core`` has a row in the Core table (a
  row whose module is gone is left to review: an in-progress module may be
  documented before it is committed);
* every entry of ``MODULE_ORDER`` is placed in the dependency-direction section,
  in the diagram or as a backticked name in its prose.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from sqlalchemy import Table

from src.api.router import MODULE_ORDER
from src.core.config import API_ROOT, REPO_ROOT
from src.core.models import Base
from src.core.registry import import_all_models

pytestmark = pytest.mark.unit

DATABASE_DOC = REPO_ROOT / "docs" / "database.md"
ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"
SOURCE = API_ROOT / "src"

TABLE_MAP_HEADING = "## 2."
CORE_HEADING = "## Core (`src/core`)"
DEPENDENCY_HEADING = "### Dependency direction"

#: ``src/common`` owns tables too (``document_sequences``) and has its own row.
COMMON = "common"

_ROW = re.compile(r"^\| `([a-z_]+)` \| (.*) \|$", re.MULTILINE)
_SNAKE_NAME = re.compile(r"`([a-z][a-z0-9_]*)`")
_HEADING = re.compile(r"^#{2,3} ", re.MULTILINE)
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def section(document: str, heading: str) -> str:
    """From the line that starts with ``heading`` to the next level-2 or -3 heading."""
    found = re.search("^" + re.escape(heading), document, re.MULTILINE)
    if found is None:
        raise AssertionError(f"no heading starting {heading!r}")
    following = _HEADING.search(document, found.end())
    return document[found.start() : following.start() if following else len(document)]


def table_rows(document: str) -> dict[str, set[str]]:
    """Section 2's rows: module -> the snake_case names its table column backticks."""
    part = section(document, TABLE_MAP_HEADING)
    return {module: set(_SNAKE_NAME.findall(cells)) for module, cells in _ROW.findall(part)}


def table_map_faults(
    rows: Mapping[str, set[str]], owners: Mapping[str, str], modules: set[str]
) -> list[str]:
    """Where section 2 and the models disagree about who owns which table."""
    faults = [
        f"{table} is defined in {owner} but not named in the `{owner}` row"
        for table, owner in sorted(owners.items())
        if table not in rows.get(owner, set())
    ]
    named = set().union(*rows.values()) if rows else set()
    faults += [
        f"the map names {name}, which no model defines" for name in sorted(named - set(owners))
    ]
    faults += [f"src/modules/{name} has no row" for name in sorted(modules - set(rows))]
    faults += [
        f"the `{name}` row names no package under src/modules"
        for name in sorted(set(rows) - modules - {COMMON})
    ]
    return faults


def core_faults(document: str, core_modules: set[str]) -> list[str]:
    """Every ``src/core`` module the Core table of the architecture document leaves out."""
    listed = {module for module, _ in _ROW.findall(section(document, CORE_HEADING))}
    return [
        f"src/core/{name} has no row in the Core table" for name in sorted(core_modules - listed)
    ]


def placement_faults(document: str, order: Iterable[str]) -> list[str]:
    """Every mounted module the dependency-direction section does not place."""
    part = section(document, DEPENDENCY_HEADING)
    placed = {word for block in _FENCE.findall(part) for word in re.findall(r"[a-z_]+", block)}
    placed |= set(_SNAKE_NAME.findall(part))
    return [
        f"{name} is mounted (MODULE_ORDER) but not placed in the dependency direction"
        for name in order
        if name not in placed
    ]


def _owner(module_name: str) -> str | None:
    """``src.modules.finance.models`` -> ``finance``; ``src.common.codes`` -> ``common``."""
    parts = module_name.split(".")
    if parts[:2] == ["src", "modules"] and len(parts) > 2:
        return parts[2]
    if parts[:2] == ["src", COMMON]:
        return COMMON
    return None


def table_owners() -> dict[str, str]:
    """Every application table on the metadata, keyed to the package that defines it.

    Mapped classes name their module; an association table (``user_roles``) is
    found as a module-level ``Table``. A table a test registers is not the
    application's and is left out, because its module is not under ``src``.
    """
    import_all_models()
    owners: dict[str, str] = {}
    for mapper in Base.registry.mappers:
        owner = _owner(mapper.class_.__module__)
        if owner is not None and isinstance(mapper.local_table, Table):
            owners[mapper.local_table.name] = owner
    for module_name, module in list(sys.modules.items()):
        owner = _owner(module_name)
        if owner is None or module is None:
            continue
        for value in vars(module).values():
            if isinstance(value, Table) and value.metadata is Base.metadata:
                owners.setdefault(value.name, owner)
    return owners


def packages(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if (path / "__init__.py").is_file()}


def core_modules() -> set[str]:
    root = SOURCE / "core"
    files = {path.stem for path in root.glob("*.py") if path.stem != "__init__"}
    return files | packages(root)


def test_the_maps_read_the_sections_they_check() -> None:
    rows = table_rows(DATABASE_DOC.read_text(encoding="utf-8"))
    assert {"inventory", "finance", COMMON} <= set(rows)
    assert "inventory_balances" in rows["inventory"]
    owners = table_owners()
    assert owners["user_roles"] == "users"
    assert owners["document_sequences"] == COMMON
    assert len(owners) > 150


def test_every_table_is_in_its_owners_row_of_the_database_map() -> None:
    faults = table_map_faults(
        table_rows(DATABASE_DOC.read_text(encoding="utf-8")),
        table_owners(),
        packages(SOURCE / "modules"),
    )
    assert not faults, (
        "docs/database.md section 2 (Module map and table ownership) and the models "
        "disagree. Add the table to its module's row, or the module's row to the map:\n"
        + "\n".join(faults)
    )


def test_every_core_module_is_in_the_core_table() -> None:
    faults = core_faults(ARCHITECTURE_DOC.read_text(encoding="utf-8"), core_modules())
    assert not faults, (
        "docs/architecture.md's Core table must say what each src/core module is for:\n"
        + "\n".join(faults)
    )


def test_every_mounted_module_is_placed_in_the_dependency_direction() -> None:
    faults = placement_faults(ARCHITECTURE_DOC.read_text(encoding="utf-8"), MODULE_ORDER)
    assert not faults, (
        "Place the module in docs/architecture.md's dependency diagram, or name it with "
        "the shared services every layer may call:\n" + "\n".join(faults)
    )


DOCUMENT = """\
## 2. Module map and table ownership

| Module | Tables |
|---|---|
| `inventory` | `inventory_balances`, `stock_batches` |
| `dispatch` | `dispatches`, `dispatch_tracking_events` |
| `common` | `document_sequences` |

### Company approval settings

| `audit` | `not_part_of_the_map` |

## Core (`src/core`)

| Module | Responsibility |
|---|---|
| `config` | settings |
| `seed` | baseline data |

## Modules

### Dependency direction

```
engines      inventory
downstream   dispatch
```

`audit` is a service every layer may call. Reports registers a rule.

## Next
"""


def test_the_table_rule_finds_a_missing_table_a_stale_name_and_a_missing_row() -> None:
    owners = {
        "inventory_balances": "inventory",
        "stock_batches": "inventory",
        "stock_batch_conversions": "inventory",
        "dispatches": "dispatch",
        "document_sequences": COMMON,
        "courier_accounts": "courier_tracking",
    }

    assert table_map_faults(
        table_rows(DOCUMENT), owners, {"inventory", "dispatch", "courier_tracking"}
    ) == [
        "courier_accounts is defined in courier_tracking but not named in the "
        "`courier_tracking` row",
        "stock_batch_conversions is defined in inventory but not named in the `inventory` row",
        "the map names dispatch_tracking_events, which no model defines",
        "src/modules/courier_tracking has no row",
    ]


def test_the_table_rule_passes_a_complete_map_and_ignores_later_tables() -> None:
    owners = {
        "inventory_balances": "inventory",
        "stock_batches": "inventory",
        "dispatches": "dispatch",
        "dispatch_tracking_events": "dispatch",
        "document_sequences": COMMON,
    }

    assert table_map_faults(table_rows(DOCUMENT), owners, {"inventory", "dispatch"}) == []
    assert "audit" not in table_rows(DOCUMENT)


def test_the_core_rule_finds_an_undocumented_module() -> None:
    assert core_faults(DOCUMENT, {"config", "seed", "readiness"}) == [
        "src/core/readiness has no row in the Core table"
    ]
    assert core_faults(DOCUMENT, {"config", "seed"}) == []


def test_the_placement_rule_reads_the_diagram_and_backticked_names_only() -> None:
    assert placement_faults(DOCUMENT, ["inventory", "dispatch", "audit"]) == []
    assert placement_faults(DOCUMENT, ["courier_tracking", "reports"]) == [
        "courier_tracking is mounted (MODULE_ORDER) but not placed in the dependency direction",
        "reports is mounted (MODULE_ORDER) but not placed in the dependency direction",
    ]
