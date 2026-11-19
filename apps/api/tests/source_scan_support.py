"""Readers for the backend source, shared by the guards that scan it.

Each ``tests/unit/test_*.py`` guard over ``src`` reads the tree the same way:
where it lives, and every Python file in it but the bytecode caches. Twenty-one
guards wrote ``SRC`` out for themselves, and eight the walk over it as well;
they read both from here now, as the frontend guards read ``WEB_SRC`` from
``tests/frontend_scan_support.py``.

A walk that answers a different question stays with its guard:
``test_source_docstrings`` lists only the files with content, as paths relative
to ``src``; ``test_session_revocation_reasons`` keys its sources by their path
from the API root; ``test_layering_guards`` and ``test_dead_definitions`` walk
whatever roots they are handed, and ``test_support_modules`` walks ``tests``.
"""

from __future__ import annotations

from pathlib import Path

#: The backend's source tree.
SRC = Path(__file__).resolve().parents[1] / "src"


def python_sources() -> list[Path]:
    """Every Python file under ``src``, sorted, the bytecode caches left out."""
    return sorted(path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts)


def source_texts() -> dict[str, str]:
    """The text of every Python file under ``src``, keyed by its path from ``src``."""
    return {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in python_sources()
    }


__all__ = ["SRC", "python_sources", "source_texts"]
