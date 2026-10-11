"""The last migration to set the report-key CHECK admits every ``ReportKey``.

``report_snapshots.report_key`` is a stored enum, and the database guards it
with ``ck_report_snapshots_report_key_is_known`` - a ``report_key IN (...)``
CHECK that spells every key out literally, because a migration states the
schema as it was and must not import an enum that keeps changing after it has
run. Adding a :class:`~src.modules.reports.models.ReportKey` member is therefore
two edits: the enum, and a migration that swaps the CHECK for the wider list.

Nothing else catches a missing swap. The test suites build their schema from
the model metadata, which already carries the new member, so every test passes;
only a database built by Alembic refuses the new report, as an integrity error
on the first request large enough to run in the background. The same happens
when two branches of work each add a key: each branch's swap lists the keys
*it* knew, and whichever migration ends up last in the chain silently drops the
other's key while both branches stay green.

So this reads the migrations the way Alembic orders them - by walking the
``down_revision`` links, never by file name, because a re-pointed revision keeps
the file name it was written under - finds the latest one that sets the CHECK,
and compares the literal list its ``upgrade()`` states with the enum. When it
fails, the fix is a small migration at the end of the chain that drops the
CHECK and re-adds it ``NOT VALID`` with the full literal list, then validates
it (``20260905_0100_report_snapshots_packing_list_key.py`` is the pattern).

No database is opened: this is source arithmetic, and it belongs in the fast loop.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from pathlib import Path

import pytest

from src.modules.reports.models import ReportKey

pytestmark = pytest.mark.unit

#: ``apps/api/migrations/versions``, from ``apps/api/tests/unit``. Tests run from
#: a checkout, where this layout is fixed.
VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"

#: The constraint's label; the naming convention prefixes ``ck_report_snapshots_``.
CONSTRAINT_LABEL = "report_key_is_known"

#: ``report_key IN ('A', 'B')`` written out as SQL text, as the initial schema
#: does. The closing parenthesis must be in the same string, so an f-string that
#: builds the list at run time never matches here.
_SQL_IN_LIST = re.compile(r"report_key\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_QUOTED = re.compile(r"'([^']*)'")


@dataclass(frozen=True)
class _Migration:
    path: Path
    revision: str
    down_revisions: tuple[str, ...]
    tree: ast.Module
    source: str


def _module_assignments(tree: ast.Module) -> dict[str, ast.expr]:
    """Every ``NAME = value`` / ``NAME: type = value`` at module level."""
    assigned: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assigned[node.target.id] = node.value
    return assigned


@cache
def _migrations() -> dict[str, _Migration]:
    found: dict[str, _Migration] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assigned = _module_assignments(tree)
        assert "revision" in assigned, f"{path.name} declares no revision"
        revision = ast.literal_eval(assigned["revision"])
        down = ast.literal_eval(assigned["down_revision"]) if "down_revision" in assigned else None
        if down is None:
            down_revisions: tuple[str, ...] = ()
        elif isinstance(down, str):
            down_revisions = (down,)
        else:
            down_revisions = tuple(down)
        assert revision not in found, f"revision {revision} is declared twice"
        found[revision] = _Migration(path, revision, down_revisions, tree, source)
    return found


def _ancestors(revision: str) -> set[str]:
    """Every revision ``revision`` builds on, through ``down_revision`` links."""
    migrations = _migrations()
    seen: set[str] = set()
    pending = list(migrations[revision].down_revisions)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        assert current in migrations, (
            f"a migration names down_revision {current!r}, which no file declares"
        )
        seen.add(current)
        pending.extend(migrations[current].down_revisions)
    return seen


def _check_migrations() -> list[_Migration]:
    """The migrations that create or alter the CHECK, oldest first.

    Ordered by ancestry. Two of them on parallel branches is an unreconciled
    merge - each side lists only the keys it knew - so that fails here rather
    than being ordered arbitrarily.
    """
    touching = [m for m in _migrations().values() if CONSTRAINT_LABEL in m.source]
    ordered = sorted(touching, key=lambda m: len(_ancestors(m.revision)))
    for older, newer in pairwise(ordered):
        assert older.revision in _ancestors(newer.revision), (
            f"{older.path.name} and {newer.path.name} both set {CONSTRAINT_LABEL} on "
            "parallel branches of the revision graph; chain them and reconcile the key list"
        )
    return ordered


def _string_values(node: ast.expr, assigned: dict[str, ast.expr]) -> tuple[str, ...] | None:
    """The strings a tuple or list expression spells, through module-level names.

    Handles the shapes the swap migrations use - ``(*_PREVIOUS_KEYS, *_NEW_KEYS)``,
    a bare name, ``A + B`` - and answers None for anything that is not a list of
    strings, so an unreadable shape fails the caller instead of reading as empty.
    """
    if isinstance(node, ast.Name):
        target = assigned.get(node.id)
        return None if target is None else _string_values(target, assigned)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_values(node.left, assigned)
        right = _string_values(node.right, assigned)
        return None if left is None or right is None else left + right
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    values: list[str] = []
    for element in node.elts:
        if isinstance(element, ast.Starred):
            spread = _string_values(element.value, assigned)
            if spread is None:
                return None
            values.extend(spread)
            continue
        if isinstance(element, ast.Name):
            target = assigned.get(element.id)
            element = target if target is not None else element
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            values.append(element.value)
            continue
        return None
    return tuple(values)


def _established_keys(migration: _Migration) -> frozenset[str]:
    """The key list the migration's ``upgrade()`` leaves the CHECK holding."""
    upgrade = next(
        (
            node
            for node in migration.tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
        ),
        None,
    )
    assert upgrade is not None, f"{migration.path.name} has no upgrade()"

    spelled = [
        match.group(1)
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        for match in [_SQL_IN_LIST.search(node.value)]
        if match is not None
    ]
    if spelled:
        assert len(spelled) == 1, (
            f"{migration.path.name} spells report_key IN (...) {len(spelled)} times in upgrade()"
        )
        return frozenset(_QUOTED.findall(spelled[0]))

    assigned = _module_assignments(migration.tree)
    passed = [
        values
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        for argument in node.args
        for values in [_string_values(argument, assigned)]
        if values
    ]
    assert len(passed) == 1, (
        f"{migration.path.name} names {CONSTRAINT_LABEL}, but its upgrade() states "
        f"{len(passed)} key lists this test can read (expected exactly one): spell the "
        "keys as a literal tuple, as 20260905_0100_report_snapshots_packing_list_key.py does"
    )
    return frozenset(passed[0])


def test_the_check_has_a_history_this_test_can_read() -> None:
    """The initial schema creates it; every later swap states a readable list."""
    ordered = _check_migrations()

    assert ordered, f"no migration mentions {CONSTRAINT_LABEL}"
    assert ordered[0].down_revisions == (), (
        f"the oldest migration naming {CONSTRAINT_LABEL} should be the initial schema, "
        f"not {ordered[0].path.name}"
    )
    for migration in ordered:
        keys = _established_keys(migration)
        assert keys, f"{migration.path.name} states an empty key list"
        assert all(key and key == key.strip() for key in keys), migration.path.name


def test_the_latest_check_lists_every_report_key() -> None:
    latest = _check_migrations()[-1]
    stated = _established_keys(latest)
    expected = {key.value for key in ReportKey}

    missing = sorted(expected - stated)
    unknown = sorted(stated - expected)
    assert not missing and not unknown, (
        f"{latest.path.name} (revision {latest.revision}) is the last migration to set "
        f"ck_report_snapshots_{CONSTRAINT_LABEL}, and its list disagrees with ReportKey. "
        f"Missing from the CHECK: {missing or 'none'}. Not in ReportKey: {unknown or 'none'}. "
        "Add a migration at the end of the chain that drops the CHECK and re-adds it "
        "NOT VALID with every ReportKey value spelled out, then VALIDATEs it."
    )
