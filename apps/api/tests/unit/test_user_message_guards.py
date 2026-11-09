"""Mechanical guards on the sentences the API hands a person.

``core/messages.py``: a message reads as a clear sentence a user can act on and
names no internals. Two shapes broke that and shipped past review:

* nine validators interpolated a list into their refusal - ``Duplicate line
  numbers: [3, 5]``, or a list of raw item UUIDs - which is Python's repr, not
  a sentence. A collection is written with ``messages.listed``;
* three copies of the background-job sanitiser wrote "failed with an
  unexpected KeyError" onto the job row, which the MRP and report screens show
  verbatim. The exception's class belongs in the log line;
  :func:`src.core.errors.failure_message` writes the row.

* two refusals spelled the reason floor out - "at least three characters" -
  beside a ``min_length=3`` that could move without them. A sentence states the
  floor by interpolating ``REASON_MIN_LENGTH``, as ``REASON_TOO_SHORT`` does.

Every rule is a function over source text, and each has a self-test that feeds
it the violation it exists to catch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.core.constants import REASON_MIN_LENGTH

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: Raises whose reader is a developer, not a user - each with its reason.
COLLECTION_RAISES_ALLOWED: dict[str, str] = {
    "core/state_machine.py": (
        "StateMachine.validate is a definition-time self-check the unit tests run; "
        "its reader is whoever just wrote the transition table"
    ),
}

_COLLECTION_BUILDERS = frozenset({"sorted", "list", "set", "tuple", "frozenset"})


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _builds_collection(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Set, ast.Tuple, ast.ListComp, ast.SetComp)):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _COLLECTION_BUILDERS
    )


def _collection_names(function: ast.AST) -> set[str]:
    """Names this function binds to a list, set, tuple or ``sorted(...)``."""
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and _builds_collection(node.value):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _builds_collection(node.value)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def interpolated_collections(source: str) -> list[str]:
    """``line N: expr`` for each raise whose message formats a collection whole."""
    found: list[tuple[int, str]] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        collections = _collection_names(function)
        for node in ast.walk(function):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            if not node.exc.args:
                continue
            for text in ast.walk(node.exc.args[0]):
                if not isinstance(text, ast.JoinedStr):
                    continue
                for part in text.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    value = part.value
                    if (isinstance(value, ast.Name) and value.id in collections) or (
                        _builds_collection(value)
                    ):
                        found.append((node.lineno, ast.unparse(value)))
    return [f"line {line}: {expr}" for line, expr in sorted(set(found))]


def _is_class_name_of_a_value(node: ast.AST) -> bool:
    """``type(exc).__name__`` or ``exc.__class__.__name__``, for anything but self/cls."""
    if not (isinstance(node, ast.Attribute) and node.attr == "__name__"):
        return False
    owner = node.value
    if (
        isinstance(owner, ast.Call)
        and isinstance(owner.func, ast.Name)
        and owner.func.id == "type"
        and owner.args
    ):
        subject = owner.args[0]
    elif isinstance(owner, ast.Attribute) and owner.attr == "__class__":
        subject = owner.value
    else:
        return False
    return not (isinstance(subject, ast.Name) and subject.id in {"self", "cls"})


def formatted_class_names(source: str) -> list[str]:
    """``line N`` for each f-string that writes a value's class name into text."""
    lines = {
        text.lineno
        for text in ast.walk(ast.parse(source))
        if isinstance(text, ast.JoinedStr)
        for part in text.values
        if isinstance(part, ast.FormattedValue) and _is_class_name_of_a_value(part.value)
    }
    return [f"line {line}" for line in sorted(lines)]


_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _text_literals(source: str) -> list[tuple[int, str]]:
    """``(line, text)`` for every string literal and f-string part but docstrings."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def spelled_reason_minimums(source: str, minimum: int) -> list[str]:
    """``line N`` for each literal that spells ``minimum`` out as a character floor."""
    spellings = [str(minimum)]
    if minimum < len(_NUMBER_WORDS):
        spellings.append(_NUMBER_WORDS[minimum])
    pattern = re.compile(rf"\bat least (?:{'|'.join(spellings)}) characters\b", re.IGNORECASE)
    lines = {line for line, text in _text_literals(source) if pattern.search(text)}
    return [f"line {line}" for line in sorted(lines)]


def test_no_message_spells_out_the_reason_minimum() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in _python_files()
        for hit in spelled_reason_minimums(path.read_text(encoding="utf-8"), REASON_MIN_LENGTH)
    ]

    assert not failures, (
        f"{len(failures)} sentence(s) spell the reason minimum ({REASON_MIN_LENGTH}) out:\n"
        + "\n".join(failures)
        + "\n\nUse src.core.messages.REASON_TOO_SHORT, or interpolate REASON_MIN_LENGTH."
    )


def test_the_minimum_rule_flags_a_spelled_floor_and_ignores_the_constant() -> None:
    source = "\n".join(
        [
            "def check(value):",
            '    """A reason holds at least three characters."""',
            "    raise ValueError('Enter a meaningful reason of at least three characters.')",
            "    raise ValueError(f'Record why, in at least 3 characters ({value}).')",
            "    raise ValueError(f'Record at least {REASON_MIN_LENGTH} characters of reason.')",
            "    # at least three characters, as the API holds it",
        ]
    )

    assert spelled_reason_minimums(source, 3) == ["line 3", "line 4"]


def test_no_refusal_formats_a_collection_whole() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in _python_files()
        if path.relative_to(SRC).as_posix() not in COLLECTION_RAISES_ALLOWED
        for hit in interpolated_collections(path.read_text(encoding="utf-8"))
    ]

    assert not failures, (
        f"{len(failures)} raise(s) write a Python collection into a message:\n"
        + "\n".join(failures)
        + "\n\nWrite it with src.core.messages.listed - 'Duplicate line numbers: 3, 5.' - "
        "and name records by their code or line number, keeping ids in details."
    )


def test_no_message_names_an_exception_class() -> None:
    failures = [
        f"  {path.relative_to(SRC).as_posix()}: {hit}"
        for path in _python_files()
        for hit in formatted_class_names(path.read_text(encoding="utf-8"))
    ]

    assert not failures, (
        f"{len(failures)} f-string(s) write an exception's class into text:\n"
        + "\n".join(failures)
        + "\n\nA job row gets src.core.errors.failure_message(exc, ...); the class goes "
        "to the log as error_type=type(exc).__name__."
    )


def test_the_allowlist_names_only_files_that_still_raise_a_collection() -> None:
    stale = [
        relative
        for relative in COLLECTION_RAISES_ALLOWED
        if not interpolated_collections((SRC / relative).read_text(encoding="utf-8"))
    ]

    assert not stale, f"Allowed but no longer needed, so drop the entry: {stale}"


def test_the_collection_rule_flags_a_list_in_a_refusal() -> None:
    source = "\n".join(
        [
            "def check(lines):",
            "    numbers = [line.number for line in lines]",
            "    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})",
            "    if duplicates:",
            "        raise ValueError(f'Duplicate line numbers: {duplicates}')",
            "    repeated: list[str] = list(codes)",
            "    raise BusinessRuleError(f'Repeated: {repeated}', details={'x': repeated})",
            "    raise ValueError(f'Unknown: {sorted(missing)}')",
            "    raise ValueError(f'Duplicate line numbers: {listed(duplicates)}.')",
            "    raise ValueError(duplicate_line_numbers(duplicates))",
            "    count = len(duplicates)",
            "    raise ValueError(f'{count} lines repeat')",
        ]
    )

    assert interpolated_collections(source) == [
        "line 5: duplicates",
        "line 7: repeated",
        "line 8: sorted(missing)",
    ]


def test_the_class_name_rule_flags_an_exception_type_in_text() -> None:
    source = "\n".join(
        [
            "message = f'The report failed with an unexpected {type(exc).__name__}.'",
            "message = f'Failed: {error.__class__.__name__}'",
            "log.error('task.failed', error_type=type(exc).__name__)",
            "def __repr__(self):",
            "    return f'<{type(self).__name__} id={self.id}>'",
            "message = failure_message(exc, 'The report', logged_against='worker log')",
        ]
    )

    assert formatted_class_names(source) == ["line 1", "line 2"]
