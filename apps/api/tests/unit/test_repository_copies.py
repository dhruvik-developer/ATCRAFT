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

A *method* is held to the same rule across modules, because the class rule
cannot see one inside otherwise different classes: procurement and production
each carried an ``exists_in_tenant`` that was the same warehouse-scoped
statement the owner already had as ``WarehouseRepository.ids_in_scope``, which
now answers both. Two methods are copies when everything but their docstrings and
their own name is the same, and they sit in two different modules - a copy
within one module is that module's own business, and dunders are skipped, since
a constructor storing its session and tenant reads nothing.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Mapping

import pytest

from tests.source_scan_support import SRC

pytestmark = pytest.mark.unit


_DOCUMENTED = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _strip_docstrings(copy: ast.AST) -> None:
    """Remove every class and function docstring inside ``copy``, in place."""
    for inner in ast.walk(copy):
        if isinstance(inner, _DOCUMENTED) and ast.get_docstring(inner) is not None:
            inner.body = inner.body[1:] or [ast.Pass()]


def _without_docstrings(node: ast.ClassDef) -> ast.ClassDef:
    """A copy of ``node`` with every class and function docstring removed."""
    copy = ast.parse(ast.unparse(node)).body[0]
    assert isinstance(copy, ast.ClassDef)
    _strip_docstrings(copy)
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


# -- methods ------------------------------------------------------------------

#: ``a::X.m = b::Y.n`` -> why that copy stands for now. Each entry must still be
#: found, so a fixed copy cannot leave its allowance behind.
METHOD_COPIES_ALLOWED: dict[str, str] = {
    "modules/dealer_portal/repository.py::CatalogueRepository.customer"
    " = modules/users/repository.py::UserRepository.dealer_customer": (
        "both re-read a whole customers row by id, which the owner's "
        "CustomerRepository.get already answers; found when this rule was added "
        "for the warehouse lookups and left for its own change, since folding it "
        "touches the dealer portal and user administration"
    ),
}


def _module_of(location: str) -> str:
    """``modules/procurement`` for every file of that module; the file elsewhere."""
    parts = location.split("::")[0].split("/")
    return "/".join(parts[:2]) if parts[0] == "modules" else "/".join(parts)


def method_shapes(source: str) -> dict[str, str]:
    """Each ``*Repository`` class's ``Class.method`` -> its shape without its name.

    The shape is the AST dump of the method - decorators, arguments and body -
    with its docstrings removed and its own name blanked, so a renamed copy is
    still a copy. Dunder methods are skipped.
    """
    shapes: dict[str, str] = {}
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.ClassDef) and node.name.endswith("Repository")):
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if member.name.startswith("__") and member.name.endswith("__"):
                continue
            bare = ast.parse(ast.unparse(member)).body[0]
            assert isinstance(bare, ast.FunctionDef | ast.AsyncFunctionDef)
            _strip_docstrings(bare)
            bare.name = ""
            shapes[f"{node.name}.{member.name}"] = ast.dump(bare)
    return shapes


def copied_methods(sources: Mapping[str, str]) -> list[str]:
    """Every group of repository methods sharing one shape across two modules or more."""
    places: dict[str, list[str]] = defaultdict(list)
    for name, source in sources.items():
        for method, shape in method_shapes(source).items():
            places[shape].append(f"{name}::{method}")
    return sorted(
        " = ".join(sorted(group))
        for group in places.values()
        if len({_module_of(location) for location in group}) > 1
    )


def test_the_method_scan_reads_the_lookups() -> None:
    found = {
        f"{name}::{method}"
        for name, source in _source_tree().items()
        for method in method_shapes(source)
    }
    assert len(found) > 1000, "method discovery found too few repository methods"
    assert "modules/warehouses/repository.py::WarehouseLookupRepository.load" in found


def test_no_repository_method_is_a_copy_from_another_module() -> None:
    copies = [
        group for group in copied_methods(_source_tree()) if group not in METHOD_COPIES_ALLOWED
    ]
    assert not copies, (
        "These repository methods are identical in different modules; a lookup two "
        f"modules need alike belongs to the module that owns the table, once: {copies}"
    )


def test_every_method_copy_allowance_is_still_needed() -> None:
    stale = sorted(set(METHOD_COPIES_ALLOWED) - set(copied_methods(_source_tree())))
    assert not stale, f"These copies are gone; delete their allowance: {stale}"


_SCOPED_PROBE = """
class {cls}(BaseRepository[Warehouse]):
    model = Warehouse

    async def {method}(self, warehouse_id):
        "{doc}"
        stmt = select(Warehouse.id).where(
            Warehouse.company_id == self.tenant.company_id{extra},
            *warehouse_scope(self.tenant, Warehouse.id),
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def {other}(self, ids):
        return await self.{other}_of(ids)
"""


def _probe(
    cls: str = "WarehouseLookupRepository",
    method: str = "exists_in_tenant",
    doc: str = "Is it ours?",
    extra: str = "",
    other: str = "codes",
) -> str:
    return _SCOPED_PROBE.format(cls=cls, method=method, doc=doc, extra=extra, other=other)


def test_the_method_rule_finds_a_copy_in_different_classes_and_names() -> None:
    """The H-04 shape: one statement inside two classes that differ elsewhere."""
    sources = {
        "modules/procurement/repository.py": _probe(other="vendors"),
        "modules/production/repository.py": _probe(
            cls="StoreLookupRepository", method="is_ours", doc="Scoped.", other="kits"
        ),
    }
    assert copied_repositories(sources) == []
    assert copied_methods(sources) == [
        "modules/procurement/repository.py::WarehouseLookupRepository.exists_in_tenant"
        " = modules/production/repository.py::StoreLookupRepository.is_ours"
    ]


def test_the_method_rule_passes_one_predicate_apart_and_one_module() -> None:
    differing = {
        "modules/procurement/repository.py": _probe(other="vendors"),
        "modules/production/repository.py": _probe(extra=", Warehouse.is_active", other="kits"),
    }
    same_module = {
        "modules/procurement/repository.py": _probe(other="vendors"),
        "modules/procurement/inbound_repository.py": _probe(other="kits"),
    }
    assert copied_methods(differing) == []
    assert copied_methods(same_module) == []


def test_the_method_rule_skips_constructors() -> None:
    init = (
        "class {cls}:\n"
        "    def __init__(self, session, tenant):\n"
        "        self.session = session\n"
        "        self.tenant = tenant\n"
    )
    sources = {
        "modules/mrp/repository.py": init.format(cls="PackingDemandRepository"),
        "modules/packing/repository.py": init.format(cls="PackingSpecRepository"),
    }
    assert copied_methods(sources) == []
