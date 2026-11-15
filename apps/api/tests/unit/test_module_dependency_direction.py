"""Module-level imports follow docs/architecture.md's dependency direction.

"Dependencies point downward only": at module level a module imports from its
own layer, from the layers above it in the diagram, and from the services every
layer may call. Where an upper module needs a lower one's answer it imports it
inside the function that needs it, so the lower module never joins the upper
one's import graph - the cycle the deferred-import seam exists to prevent.
Inventory (an engine) had come to import allocation's and sales' repositories,
MRP (planning) packing's models, and job work and procurement the reports
module, all at module level, and nothing failed, because the diagram is prose.

The layers and the shared services are read from the document itself, so the
diagram and the code cannot drift apart without this file saying so.
``tests/unit/test_module_maps.py`` checks that every mounted module is placed;
this checks that the imports respect the placement.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from src.core.config import API_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

ARCHITECTURE_DOC = REPO_ROOT / "docs" / "architecture.md"
MODULES = API_ROOT / "src" / "modules"

DEPENDENCY_HEADING = "### Dependency direction"
SHARED_SENTENCE = "every layer may call"

_HEADING = re.compile(r"^#{2,3} ", re.MULTILINE)
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_LAYER_LINE = re.compile(r"^([a-z]+)\s{2,}(\S.*)$")
_SNAKE_NAME = re.compile(r"`([a-z][a-z0-9_]*)`")


def dependency_section(document: str) -> str:
    """From the dependency-direction heading to the next level-2 or -3 heading."""
    found = re.search("^" + re.escape(DEPENDENCY_HEADING), document, re.MULTILINE)
    if found is None:
        raise AssertionError(f"no heading starting {DEPENDENCY_HEADING!r}")
    following = _HEADING.search(document, found.end())
    return document[found.start() : following.start() if following else len(document)]


def layers(document: str) -> list[set[str]]:
    """The diagram's layers, top first: each line ``label  a · b · c`` is one layer."""
    fences = _FENCE.findall(dependency_section(document))
    if not fences:
        raise AssertionError("the dependency-direction section has no diagram")
    found: list[set[str]] = []
    for line in fences[0].splitlines():
        match = _LAYER_LINE.match(line.strip())
        if match is not None:
            found.append({name.strip() for name in match.group(2).split("·") if name.strip()})
    return found


def shared_services(document: str) -> set[str]:
    """The backticked modules of the sentence naming the services every layer may call.

    The prose is wrapped, so whitespace is folded before the sentence is sought.
    """
    part = " ".join(dependency_section(document).split())
    for sentence in re.split(r"(?<=\.)\s+", part):
        if SHARED_SENTENCE in sentence:
            return set(_SNAKE_NAME.findall(sentence))
    raise AssertionError(f"no sentence saying which services {SHARED_SENTENCE}")


def layer_of(table: list[set[str]]) -> dict[str, int]:
    return {name: index for index, names in enumerate(table) for name in names}


def _imported_modules(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        if node.module == "src.modules":
            return [f"src.modules.{alias.name}" for alias in node.names]
        return [node.module]
    return []


def downward_imports(
    source: str, owner: str, depth: Mapping[str, int], shared: set[str]
) -> list[str]:
    """Module-level imports of a module in a lower layer than ``owner``'s.

    Only statements at module level count: an import inside a function is the
    sanctioned seam, and one under ``if TYPE_CHECKING:`` never runs.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        for name in _imported_modules(node):
            parts = name.split(".")
            if parts[:2] != ["src", "modules"] or len(parts) < 3:
                continue
            other = parts[2]
            if other == owner or other in shared or other not in depth:
                continue
            if depth[other] > depth[owner]:
                found.append(f"line {node.lineno}: {name}")
    return found


def _packages() -> set[str]:
    return {path.name for path in MODULES.iterdir() if (path / "__init__.py").is_file()}


def test_the_diagram_reads_as_layers_and_shared_services() -> None:
    document = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    table = layers(document)
    assert len(table) >= 5, f"too few layers read from the diagram: {table}"
    depth = layer_of(table)
    assert depth["warehouses"] < depth["inventory"] < depth["sales"] < depth["mrp"]
    assert depth["mrp"] < depth["job_work"] < depth["reports"] < depth["dealer_portal"]
    assert {"audit", "notifications", "documents"} <= shared_services(document)


def test_every_module_package_has_a_place() -> None:
    document = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    placed = set(layer_of(layers(document))) | shared_services(document)
    unplaced = sorted(_packages() - placed)
    assert not unplaced, (
        "Place these packages in docs/architecture.md's dependency diagram, or name "
        f"them with the services every layer may call: {unplaced}"
    )


def test_module_level_imports_point_up_or_across() -> None:
    document = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    depth = layer_of(layers(document))
    shared = shared_services(document)
    offenders: dict[str, list[str]] = {}
    for path in sorted(MODULES.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        owner = path.relative_to(MODULES).parts[0]
        if owner not in depth:
            continue
        found = downward_imports(path.read_text(encoding="utf-8"), owner, depth, shared)
        if found:
            offenders[path.relative_to(MODULES).as_posix()] = found
    assert not offenders, (
        "These module-level imports reach a lower layer than their module's in "
        "docs/architecture.md. Import inside the function that needs it (the "
        "deferred-import seam), or move the module in the diagram if the "
        f"dependency is the intended design: {offenders}"
    )


# -- the rules read and reject what they exist to -----------------------------

_DOCUMENT = """\
### Dependency direction

```
masters      warehouses · products
   ↓
engines      inventory
   ↓
demand       sales
```

Dependencies point downward only. `audit` and `documents` are services every
layer may call.

### Next
"""


def test_the_reader_takes_layers_from_the_diagram_and_services_from_the_sentence() -> None:
    assert layers(_DOCUMENT) == [{"warehouses", "products"}, {"inventory"}, {"sales"}]
    assert shared_services(_DOCUMENT) == {"audit", "documents"}


def test_the_rule_rejects_a_module_level_import_from_a_lower_layer_only() -> None:
    depth = {"products": 0, "inventory": 1, "sales": 2}
    source = (
        "from typing import TYPE_CHECKING\n"
        "from src.modules.sales.repository import SalesOrderRepository\n"
        "import src.modules.sales.models\n"
        "from src.modules import sales\n"
        "from src.modules.products.models import Item\n"
        "from src.modules.inventory.models import StockSerial\n"
        "from src.modules.audit.service import record_audit\n"
        "if TYPE_CHECKING:\n"
        "    from src.modules.sales.models import SalesOrder\n"
        "def check():\n"
        "    from src.modules.sales.repository import SalesOrderLineRepository\n"
    )
    assert downward_imports(source, "inventory", depth, {"audit"}) == [
        "line 2: src.modules.sales.repository",
        "line 3: src.modules.sales.models",
        "line 4: src.modules.sales",
    ]


def test_the_rule_reads_the_real_diagram_the_way_the_finding_did() -> None:
    document = ARCHITECTURE_DOC.read_text(encoding="utf-8")
    depth = layer_of(layers(document))
    shared = shared_services(document)
    source = "from src.modules.allocation.repository import ReallocationRequestRepository\n"
    assert downward_imports(source, "inventory", depth, shared) == [
        "line 1: src.modules.allocation.repository"
    ]
    assert downward_imports(source, "sales", depth, shared) == []


def test_a_package_outside_the_diagram_is_never_silently_skipped() -> None:
    assert Path(MODULES).is_dir()
    assert {"inventory", "reports", "audit"} <= _packages()
