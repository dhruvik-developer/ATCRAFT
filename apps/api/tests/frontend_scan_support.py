"""Readers for the frontend source, shared by the frontend guards.

Each ``tests/unit/test_frontend_*.py`` scans ``apps/web/src`` for one rule, and
they read the source the same way: where it lives, which line an offset is on,
where a string literal or an expression ends, the source with its comments
blanked, and the literals a title can show. Those readers used to be defined in
one guard and imported from it by the next - ``WEB_SRC`` and ``_line`` were also
written out again in more than thirty of them - so renaming a guard broke rules
that never mentioned it. They live here now, and each guard imports what it
reads from this module alone (``tests/unit/test_support_modules.py``).

A reader that answers a different question under the same name stays with its
guard: ``test_frontend_success_builders._in_comment`` also treats a trailing
``//`` before the offset as a comment, which the one here does not.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The frontend's source tree.
WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"
#: Generated from the API's OpenAPI document; its descriptions are not screens.
GENERATED = WEB_SRC / "types" / "openapi.ts"

#: Components whose confirm button follows ``destructive`` and whose title asks.
CONFIRM_COMPONENTS = ("ConfirmDialog", "ReasonDialog")

_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _line(source: str, offset: int) -> int:
    """The 1-based line ``offset`` falls on."""
    return source.count("\n", 0, offset) + 1


def _in_comment(source: str, offset: int) -> bool:
    """Whether ``offset`` is on a line that opens as a comment (``//``, ``*``, ``/*``)."""
    line_start = source.rfind("\n", 0, offset) + 1
    return source[line_start:].lstrip().startswith(("//", "*", "/*"))


def tsx_sources() -> list[Path]:
    """Every ``.tsx`` file of the product, tests left out."""
    return sorted(
        path
        for path in WEB_SRC.rglob("*.tsx")
        if "node_modules" not in path.parts and ".test." not in path.name
    )


# -----------------------------------------------------------------------------
# Strings and expressions
# -----------------------------------------------------------------------------


def _skip_string(source: str, start: int) -> int:
    """The offset just past the quoted literal that opens at ``start``."""
    quote = source[start]
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if quote == "`" and source.startswith("${", index):
            index = _expression_end(source, index + 2, stop_at_comma=False) + 1
            continue
        index += 1
    return index


def _indent(source: str, offset: int) -> int:
    line_start = source.rfind("\n", 0, offset) + 1
    line = source[line_start:]
    return len(line) - len(line.lstrip(" "))


def _expression_end(
    source: str, start: int, *, stop_at_comma: bool, statement_indent: int | None = None
) -> int:
    """Where the expression starting at ``start`` ends.

    That is the first ``,`` at its own depth (for a property value) or the
    bracket that closes the one it sits in - so an object property's value is
    read whole however many lines its arrow body spans. A statement outside
    any bracket (``const reportFailure = (error) =>`` and its indented body)
    ends instead at the first line indented no deeper than it, which is how
    Prettier lays out a continuation.
    """
    stack: list[str] = []
    index = start
    while index < len(source):
        if (
            statement_indent is not None
            and not stack
            and source[index] == "\n"
            and index + 1 < len(source)
            and source[index + 1 :].strip()
            and _indent(source, index + 1) <= statement_indent
        ):
            return index
        char = source[index]
        if char in "'\"`":
            index = _skip_string(source, index)
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline == -1 else newline
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = len(source) if close == -1 else close + 2
            continue
        if char in "([{":
            stack.append(char)
        elif char in _CLOSERS:
            if not stack:
                return index
            stack.pop()
        elif char == "," and stop_at_comma and not stack:
            return index
        index += 1
    return index


# -----------------------------------------------------------------------------
# Comments
# -----------------------------------------------------------------------------


def blank_comments(source: str) -> str:
    """``source`` with every comment blanked to spaces; strings and newlines kept.

    A quoted string cannot span lines, so one ends at a newline: an apostrophe
    in JSX text ("Don't") then costs the scan the rest of that line rather than
    the rest of the file. ``${...}`` inside a template is read as code, comments
    and all.
    """
    out = list(source)
    length = len(source)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if out[index] != "\n":
                out[index] = " "

    def comment_end(index: int) -> int | None:
        """Where the comment opening at ``index`` ends, blanking it; None if none opens."""
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end == -1 else end + 2
        else:
            return None
        blank(index, end)
        return end

    def string_end(index: int) -> int:
        quote = source[index]
        index += 1
        while index < length:
            char = source[index]
            if char == "\\":
                index += 2
                continue
            if char == quote:
                return index + 1
            if char == "\n" and quote != "`":
                return index
            if quote == "`" and source.startswith("${", index):
                index = code_end(index + 2, closing_brace=True)
                continue
            index += 1
        return length

    def code_end(index: int, *, closing_brace: bool) -> int:
        depth = 0
        while index < length:
            end = comment_end(index)
            if end is not None:
                index = end
                continue
            char = source[index]
            if char in "'\"`":
                index = string_end(index)
                continue
            if closing_brace and char == "{":
                depth += 1
            elif closing_brace and char == "}":
                if depth == 0:
                    return index + 1
                depth -= 1
            index += 1
        return length

    code_end(0, closing_brace=False)
    return "".join(out)


def _past_comment(text: str, index: int) -> int:
    """The offset after a comment opening at ``index``, or ``index`` if none opens there."""
    if text.startswith("//", index):
        newline = text.find("\n", index)
        return len(text) if newline == -1 else newline
    if text.startswith("/*", index):
        close = text.find("*/", index + 2)
        return len(text) if close == -1 else close + 2
    return index


# -----------------------------------------------------------------------------
# What a title shows
# -----------------------------------------------------------------------------

_COMPARED = re.compile(r"(?:===|!==|==|!=)\s*$")
_ARGUMENT = re.compile(r"[(,]\s*$")
_CONCATENATED = re.compile(r"^\s*\+")


def _shown_literals(expression: str) -> list[str]:
    """The literals of ``expression`` that can end up as the title's text."""
    shown = []
    index = 0
    while index < len(expression):
        if (skipped := _past_comment(expression, index)) != index:
            index = skipped
            continue
        if expression[index] not in "'\"`":
            index += 1
            continue
        end = _skip_string(expression, index)
        text = expression[index + 1 : end - 1]
        if (
            text.strip()
            and not _COMPARED.search(expression[:index])
            and not _ARGUMENT.search(expression[:index])
            and not _CONCATENATED.match(expression[end:])
        ):
            shown.append(text)
        index = end
    return shown
