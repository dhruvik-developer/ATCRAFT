"""Every source file says what it is, and every long branching function its rule.

Meaningful comments on non-obvious logic (standard 6), held where the logic is
hardest to recover by reading:

* A module docstring is the one place a file says what it owns and why it is
  separate. Eight files had none - the whole sales-returns module and three of
  finance's credit-note files.
* A long function that branches a lot enforces a rule nobody can learn except
  by tracing every branch. A purchase order's duplicate resolution ran 249
  lines through 29 branches, and a sales return's transition 256 lines through
  20, with no account of the rule either enforced; nineteen such functions had
  no docstring.

The function rule is about *branching* length on purpose: a long function that
only lists data - a demo seed's specifications, a report's column projection -
has no rule to explain, so it needs both thresholds to be caught.
"""

from __future__ import annotations

import ast

import pytest

from tests.source_scan_support import SRC

pytestmark = pytest.mark.unit


#: A function longer than this many lines ...
LONG_FUNCTION_LINES = 80
#: ... with at least this many branches (if/for/while/try/with/match) states its rule.
BRANCHY_FUNCTION_BRANCHES = 10

_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)


def _source_files() -> list[str]:
    return sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "__pycache__" not in path.parts and path.read_text(encoding="utf-8").strip()
    )


def missing_module_docstring(source: str) -> bool:
    return bool(source.strip()) and ast.get_docstring(ast.parse(source)) is None


def undocumented_branchy_functions(source: str) -> list[str]:
    """Functions over the length threshold with enough branches and no docstring."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        length = (node.end_lineno or node.lineno) - node.lineno + 1
        branches = sum(
            isinstance(child, _BRANCHES) for child in ast.walk(node) if child is not node
        )
        if (
            length > LONG_FUNCTION_LINES
            and branches >= BRANCHY_FUNCTION_BRANCHES
            and ast.get_docstring(node) is None
        ):
            found.append(f"line {node.lineno}: {node.name} ({length} lines, {branches} branches)")
    return sorted(found, key=lambda finding: int(finding.split(":")[0].removeprefix("line ")))


def test_the_scan_reads_the_source_tree() -> None:
    files = _source_files()
    assert len(files) > 250, "source discovery found too few files"
    assert "modules/sales_returns/service.py" in files


def test_every_source_file_has_a_module_docstring() -> None:
    missing = [
        name
        for name in _source_files()
        if missing_module_docstring((SRC / name).read_text(encoding="utf-8"))
    ]
    assert not missing, (
        "Open each file with a docstring saying what it owns and why it is a file of "
        f"its own: {missing}"
    )


def test_every_long_branching_function_states_its_rule() -> None:
    offenders = {
        name: found
        for name in _source_files()
        if (found := undocumented_branchy_functions((SRC / name).read_text(encoding="utf-8")))
    }
    assert not offenders, (
        f"A function over {LONG_FUNCTION_LINES} lines with {BRANCHY_FUNCTION_BRANCHES} or "
        "more branches needs a docstring stating the rule it enforces, what it checks and "
        f"what it changes: {offenders}"
    )


# -- the rules reject what they exist to catch --------------------------------


def _branchy_body(count: int) -> str:
    """A function body of ``count`` ifs, each two lines long."""
    return "".join(f"    if x == {index}:\n        x += 1\n" for index in range(count))


def test_the_module_rule_rejects_a_file_that_opens_with_code() -> None:
    assert missing_module_docstring("from __future__ import annotations\nx = 1\n")
    assert not missing_module_docstring('"""Owns x."""\n\nx = 1\n')
    assert not missing_module_docstring("\n")


def test_the_function_rule_needs_both_length_and_branching() -> None:
    long_and_branchy = "async def decide(x):\n" + _branchy_body(41)
    long_and_flat = "def specs():\n" + "".join(f"    row_{i} = {i}\n" for i in range(90))
    short_and_branchy = "def pick(x):\n" + _branchy_body(12)
    documented = 'def decide(x):\n    """The rule."""\n' + _branchy_body(41)
    assert undocumented_branchy_functions(long_and_branchy) == [
        "line 1: decide (83 lines, 41 branches)"
    ]
    assert undocumented_branchy_functions(long_and_flat) == []
    assert undocumented_branchy_functions(short_and_branchy) == []
    assert undocumented_branchy_functions(documented) == []
