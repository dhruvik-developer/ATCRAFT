"""No definition in src that nothing reads.

Thirty-nine definitions were found referenced by nothing but themselves and
their ``__all__`` entry - not by src, not by a test, not by a script or a
migration - and several misled: ``Principal.can_access_category`` checked a
category id exactly while the live rule walks the lineage, ``DEBIT_NORMAL_TYPES``
claimed to decide how balances are presented, and three ``MAX_*_ROWS`` constants
described bounds nothing enforced. Each was deleted; this keeps the next one
from accumulating silently.

The rule is deliberately coarse and conservative. A name counts as read if the
word appears anywhere else in the Python corpus - in code, a string or a
comment - so it never flags a name that is used, only one that is mentioned
nowhere. Decorated functions and classes are skipped (a route, a validator, a
task or an event listener is reached through its decorator), as are dunders.
What remains and is still legitimate is listed with the reason.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest

from src.core.config import API_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

SRC = API_ROOT / "src"

#: Every Python file whose words count as a reference.
CORPUS_ROOTS = (
    API_ROOT / "src",
    API_ROOT / "tests",
    API_ROOT / "scripts",
    API_ROOT / "migrations",
    REPO_ROOT / "scripts",
    REPO_ROOT / "tests" / "load",
)

#: ``path::name`` -> why it stays although nothing names it.
UNREAD_ALLOWED: dict[str, str] = {
    "core/types.py::load_dialect_impl": (
        "SQLAlchemy TypeDecorator hook: the dialect calls it, no code does"
    ),
    "modules/inventory/models.py::LAYER_ORIGIN_MIGRATION": (
        "a persisted value: the opening cost layers the FIFO migration wrote carry "
        "origin 'MIGRATION', and the constant names that vocabulary beside "
        "LAYER_ORIGIN_RECONCILIATION"
    ),
}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: Module-level names a framework or the application factory reads by name.
_CONVENTIONAL = frozenset({"router", "log", "logger", "app", "pytestmark"})


def _python_files(roots: Iterable[Path]) -> list[Path]:
    return sorted(
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def word_counts(sources: Iterable[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for source in sources:
        counts.update(_WORD.findall(source))
    return counts


def _exported(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            names |= {item.value for item in node.value.elts if isinstance(item, ast.Constant)}
    return names


def definitions(source: str) -> list[tuple[str, int]]:
    """Top-level functions, classes and assignments, and undecorated methods."""
    found: list[tuple[str, int]] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            # A decorator may register what it decorates (a route, a task), so a
            # decorated name is not judged - but a class decorator such as
            # @dataclass registers nothing of its methods, which still are.
            if not node.decorator_list:
                found.append((node.name, node.lineno))
            if isinstance(node, ast.ClassDef):
                found.extend(
                    (item.name, item.lineno)
                    for item in node.body
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                    and not item.decorator_list
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found.extend(
                (target.id, node.lineno) for target in targets if isinstance(target, ast.Name)
            )
    return [
        (name, line)
        for name, line in found
        if not name.startswith("__") and name not in _CONVENTIONAL
    ]


def unread_definitions(source: str, corpus: Counter[str]) -> list[str]:
    """Definitions in ``source`` whose name appears nowhere but at the definition.

    ``corpus`` counts the definition itself, and an ``__all__`` entry is not a
    reader, so a name read anywhere has a count of at least two beyond its export.
    """
    exported = _exported(ast.parse(source))
    return [
        f"line {line}: {name}"
        for name, line in definitions(source)
        if corpus[name] - (1 if name in exported else 0) <= 1
    ]


@pytest.fixture(scope="module")
def corpus() -> Counter[str]:
    """Every word of the corpus but this file, whose allowances and history name
    the very definitions it judges."""
    here = Path(__file__).resolve()
    return word_counts(
        path.read_text(encoding="utf-8")
        for path in _python_files(CORPUS_ROOTS)
        if path.resolve() != here
    )


def _unread_in_src(corpus: Counter[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in _python_files([SRC]):
        name = path.relative_to(SRC).as_posix()
        unread = unread_definitions(path.read_text(encoding="utf-8"), corpus)
        if unread:
            found[name] = unread
    return found


def test_every_definition_in_src_is_read_somewhere(corpus: Counter[str]) -> None:
    offenders = {
        name: [
            finding
            for finding in findings
            if f"{name}::{finding.split(': ', 1)[1]}" not in UNREAD_ALLOWED
        ]
        for name, findings in _unread_in_src(corpus).items()
    }
    offenders = {name: findings for name, findings in offenders.items() if findings}
    assert not offenders, (
        "Nothing in src, tests, scripts or migrations reads these. Delete them (and "
        "their __all__ entries), or wire a constant to the bound it describes; list "
        f"one in UNREAD_ALLOWED only with the reason it must stay: {offenders}"
    )


def test_every_allowance_is_still_unread(corpus: Counter[str]) -> None:
    unread = {
        f"{name}::{finding.split(': ', 1)[1]}"
        for name, findings in _unread_in_src(corpus).items()
        for finding in findings
    }
    stale = sorted(set(UNREAD_ALLOWED) - unread)
    assert not stale, f"remove these UNREAD_ALLOWED entries, something reads them now: {stale}"


# -- the rule rejects what it exists to catch ----------------------------------


def test_the_rule_flags_an_unread_name_and_spares_a_read_or_decorated_one() -> None:
    source = (
        "MAX_ROWS = 500\n"
        "LIVE_LIMIT = 10\n"
        "def helper():\n"
        "    return LIVE_LIMIT\n"
        "def orphan():\n"
        "    return 1\n"
        "@router.get('/x')\n"
        "async def handler():\n"
        "    return helper()\n"
        "class Repo:\n"
        "    def used(self):\n"
        "        return 1\n"
        "    def unused(self):\n"
        "        return 2\n"
        "    def __init__(self):\n"
        "        self.used()\n"
        "@dataclass(frozen=True)\n"
        "class Facts:\n"
        "    def stale_check(self):\n"
        "        return 3\n"
        "__all__ = ['MAX_ROWS', 'Repo', 'orphan']\n"
    )
    corpus = word_counts([source, "Repo().used()\n"])
    assert unread_definitions(source, corpus) == [
        "line 1: MAX_ROWS",
        "line 5: orphan",
        "line 13: unused",
        "line 19: stale_check",
    ]
