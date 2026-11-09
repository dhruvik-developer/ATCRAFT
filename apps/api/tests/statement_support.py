"""Count what a block of code asks the database, for tests that pin a statement budget.

A read that labels its rows through ``labels_by_id`` costs one statement per
referenced table per page, and a batched check one per document. One statement
per row is the failure these tests exist to catch: invisible on the two-row page
a test writes, and two hundred round trips on the page a person opens. The
check is comparative - a larger page or document costs what a smaller one does -
so it does not break each time an unrelated read joins the request.

It lives here rather than in each suite that needs it, so a budget test is one
import away instead of one more pasted copy of the listener.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event


@contextmanager
def counted(engine: Any) -> Iterator[list[str]]:
    """Every statement ``engine`` executes while the block runs, in order."""
    statements: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def selects(statements: list[str]) -> int:
    """How many of ``statements`` are reads."""
    return sum(1 for text in statements if text.lstrip().upper().startswith("SELECT"))


__all__ = ["counted", "selects"]
