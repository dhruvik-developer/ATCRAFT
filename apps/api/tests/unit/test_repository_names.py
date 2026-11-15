"""A module's own repository never takes the name of its owner's.

``src/common/repository.py`` lets a module read another module's table through
a lookup of its own, and ``test_repository_copies`` stops that lookup being a
copy. This guards the name. ``FinancialYearRepository`` meant organization's
year with its own rules in one import and finance's one-method posting-period
read in another; ``ItemVendorLinkRepository`` meant products' link maintenance
or procurement's purchase-history writes; ``SalesOrderRepository`` and
``SalesOrderLineRepository`` meant sales' order or dispatch's locks. Two classes
under one name over one table, with different methods, is an import away from
calling a method one of them does not have - or the wrong rules.

A repository over a model another module owns (defines in a ``models*.py``)
may not take a class name that module defines. The one exception is a
``*LookupRepository``: the documented name for a reader's lookup, which several
modules share on purpose.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Mapping

import pytest

from src.core.config import API_ROOT

pytestmark = pytest.mark.unit

MODULES = API_ROOT / "src" / "modules"

#: The documented same-name lookup convention (``src/common/repository.py``).
LOOKUP_SUFFIX = "LookupRepository"


def owner_named(sources: Mapping[str, str]) -> list[str]:
    """Each reader's repository that takes a class name the model's owner defines.

    ``sources`` maps a path under ``src/modules`` (``"finance/repository.py"``)
    to its text. A model's owner is the module whose ``models*.py`` defines it.
    """
    owner: dict[str, str] = {}
    defined: dict[str, set[str]] = defaultdict(set)
    repositories: list[tuple[str, str, str]] = []
    for path, source in sources.items():
        module, filename = path.split("/", 1)[0], path.rsplit("/", 1)[-1]
        for node in ast.parse(source).body:
            if not isinstance(node, ast.ClassDef):
                continue
            defined[module].add(node.name)
            if filename.startswith("models"):
                owner.setdefault(node.name, module)
            if node.name.endswith("Repository") and node.bases:
                base = node.bases[0]
                if isinstance(base, ast.Subscript) and isinstance(base.slice, ast.Name):
                    repositories.append((module, node.name, base.slice.id))
    return sorted(
        f"{module}::{name} over {model} takes a class name the {owner[model]} module defines"
        for module, name, model in repositories
        if model in owner
        and owner[model] != module
        and name in defined[owner[model]]
        and not name.endswith(LOOKUP_SUFFIX)
    )


def _module_sources() -> dict[str, str]:
    return {
        path.relative_to(MODULES).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(MODULES.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_the_scan_reads_owners_and_repositories() -> None:
    sources = _module_sources()
    assert "organization/repository.py" in sources and "sales/models.py" in sources
    assert sum(source.count("Repository(BaseRepository[") for source in sources.values()) > 150


def test_no_module_names_its_repository_after_the_owners() -> None:
    clashes = owner_named(_module_sources())
    assert not clashes, (
        "A repository over another module's table takes that module's own class name; "
        f"name it for what it reads, or end it in {LOOKUP_SUFFIX}: {clashes}"
    )


# -- the rule rejects what it exists to catch ---------------------------------

_OWNER = {
    "organization/models.py": "class FinancialYear(Base):\n    pass\n",
    "organization/repository.py": (
        "class FinancialYearRepository(BaseRepository[FinancialYear]):\n    model = FinancialYear\n"
    ),
}


def _reader(name: str, model: str = "FinancialYear") -> dict[str, str]:
    return {
        **_OWNER,
        "finance/repository.py": f"class {name}(BaseRepository[{model}]):\n    model = {model}\n",
    }


def test_a_reader_taking_the_owners_name_is_named() -> None:
    assert owner_named(_reader("FinancialYearRepository")) == [
        "finance::FinancialYearRepository over FinancialYear takes a class name the"
        " organization module defines"
    ]


def test_a_reader_named_for_what_it_reads_passes() -> None:
    assert owner_named(_reader("PostingPeriodRepository")) == []


def test_a_lookup_may_share_the_owners_name() -> None:
    sources = {
        **_reader("FinancialYearLookupRepository"),
        "organization/lookups.py": "class FinancialYearLookupRepository:\n    pass\n",
    }
    assert owner_named(sources) == []


def test_the_owner_naming_its_own_repository_passes() -> None:
    assert owner_named(_OWNER) == []
