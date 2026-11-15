"""No repository class is a copy of one defined elsewhere.

``src/common/repository.py``'s module docstring records the decision this
guards: a module reads another module's facts through a small lookup of its
own, accepting duplicated *names* - several modules define an
``ItemLookupRepository``, each with the methods its documents need - to keep the
owner's rules and imports out of the reader. What it does not accept is a
duplicated *class*. Assembly, job work, packing and quality each carried a
``WarehouseLookupRepository`` whose one method was the same statement, so a fix
to it had to be found and made four times; it is now defined once, by the owner,
in ``src/modules/warehouses/repository.py``.

Two classes are copies when their bases, decorators and every member are the
same once docstrings are removed - compared by AST, so formatting and comments
do not hide a copy, and two lookups differing by one predicate are not one.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Mapping

import pytest

from src.core.config import API_ROOT

pytestmark = pytest.mark.unit

SRC = API_ROOT / "src"

_DOCUMENTED = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _without_docstrings(node: ast.ClassDef) -> ast.ClassDef:
    """A copy of ``node`` with every class and function docstring removed."""
    copy = ast.parse(ast.unparse(node)).body[0]
    assert isinstance(copy, ast.ClassDef)
    for inner in ast.walk(copy):
        if isinstance(inner, _DOCUMENTED) and ast.get_docstring(inner) is not None:
            inner.body = inner.body[1:] or [ast.Pass()]
    return copy


def repository_shapes(source: str) -> dict[str, str]:
    """Each top-level ``*Repository`` class's name -> its shape without the name.

    The shape is the AST dump of the whole class - bases, keywords, decorators
    and body - with its docstrings removed and its name blanked.
    """
    shapes: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Repository"):
            bare = _without_docstrings(node)
            bare.name = ""
            shapes[node.name] = ast.dump(bare)
    return shapes


def copied_repositories(sources: Mapping[str, str]) -> list[str]:
    """Every group of repository classes sharing one shape, as ``a::X = b::Y``."""
    places: dict[str, list[str]] = defaultdict(list)
    for name, source in sources.items():
        for cls, shape in repository_shapes(source).items():
            places[shape].append(f"{name}::{cls}")
    return sorted(" = ".join(sorted(group)) for group in places.values() if len(group) > 1)


def _source_tree() -> dict[str, str]:
    return {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def test_the_scan_reads_the_repositories() -> None:
    found = {
        f"{name}::{cls}"
        for name, source in _source_tree().items()
        for cls in repository_shapes(source)
    }
    assert len(found) > 150, "repository discovery found too few classes"
    assert "modules/warehouses/repository.py::WarehouseLookupRepository" in found


def test_no_repository_class_is_a_copy() -> None:
    copies = copied_repositories(_source_tree())
    assert not copies, (
        "These repository classes are identical; a lookup two modules need alike "
        f"belongs to the module that owns the table, once: {copies}"
    )


# -- the rule rejects what it exists to catch ---------------------------------

_LOOKUP = '''
class WarehouseLookupRepository(BaseRepository[Warehouse]):
    """{doc}"""

    model = Warehouse

    async def load(self, ids):
        """Batched id -> (code, is_active)."""
        stmt = select(Warehouse.id).where(Warehouse.company_id == self.tenant.company_id{extra})
        return await self.session.execute(stmt)
'''


def _lookup(doc: str = "Checks for the header.", extra: str = "", name: str = "") -> str:
    source = _LOOKUP.format(doc=doc, extra=extra)
    return source.replace("WarehouseLookupRepository", name) if name else source


def test_the_rule_finds_a_copy_whatever_its_docstrings_and_name_say() -> None:
    sources = {
        "modules/assembly/repository.py": _lookup("Both warehouses on the header."),
        "modules/packing/repository.py": _lookup("The warehouse on the header."),
        "modules/quality/repository.py": _lookup(name="StoreLookupRepository"),
    }
    assert copied_repositories(sources) == [
        "modules/assembly/repository.py::WarehouseLookupRepository"
        " = modules/packing/repository.py::WarehouseLookupRepository"
        " = modules/quality/repository.py::StoreLookupRepository"
    ]


def test_the_rule_passes_lookups_that_differ_in_one_predicate_or_base() -> None:
    other_base = _lookup().replace("BaseRepository[Warehouse]", "BaseRepository[Item]")
    sources = {
        "modules/assembly/repository.py": _lookup(),
        "modules/production/repository.py": _lookup(extra=", Warehouse.is_active"),
        "modules/dispatch/repository.py": other_base,
    }
    assert copied_repositories(sources) == []


def test_the_rule_reads_only_repository_classes() -> None:
    schema = "class WarehouseRead(BaseModel):\n    code: str\n"
    assert copied_repositories({"a/schemas.py": schema, "b/schemas.py": schema}) == []
