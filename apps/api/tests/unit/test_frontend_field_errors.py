"""The frontend has exactly one ``applyFieldErrors``.

``apps/web/src/lib/form-errors.ts`` is where the API's ``details.fields`` map is
turned into react-hook-form ``setError`` calls - dotted array paths, index-level
paths and the ``_root`` orphans alike. That mapping is the contract between the
exception handlers in :mod:`src.core.exception_handlers` and every form on
screen, which is why a second copy is worse than a bug: two copies agree on the
day the second one is pasted and disagree the first time either changes.

The single-copy rule used to be a comment. A comment cannot fail a build; this
can. It lives in the backend suite for the same reason the permission guards do:
the backend owns the error shape, and the frontend's reading of it is what
drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WEB_SRC = Path(__file__).resolve().parents[3] / "web" / "src"
CANONICAL = WEB_SRC / "lib" / "form-errors.ts"

#: A function declaration or a bound arrow/function expression by that name.
_DEFINITION = re.compile(
    r"\b(?:function\s+applyFieldErrors\b|(?:const|let|var)\s+applyFieldErrors\s*=)"
)


def _sources() -> list[Path]:
    return [
        source
        for source in WEB_SRC.rglob("*.ts*")
        if "node_modules" not in source.parts and source.suffix in {".ts", ".tsx"}
    ]


def test_the_canonical_copy_is_where_the_forms_import_it_from() -> None:
    """If this moves, the guard below would pass on an empty set of copies."""
    assert CANONICAL.exists(), f"{CANONICAL} is gone; every form imports from it"
    assert _DEFINITION.search(CANONICAL.read_text(encoding="utf-8")), (
        "lib/form-errors.ts no longer defines applyFieldErrors; this guard is "
        "looking at the wrong file"
    )


def test_no_feature_carries_its_own_copy() -> None:
    copies = [
        source.relative_to(WEB_SRC).as_posix()
        for source in _sources()
        if source != CANONICAL and _DEFINITION.search(source.read_text(encoding="utf-8"))
    ]
    assert not copies, (
        "applyFieldErrors is defined outside lib/form-errors.ts:\n  "
        + "\n  ".join(copies)
        + "\n\nImport it from '@/lib/form-errors' instead. Two copies of the error "
        "mapping agree on the day the second is pasted and disagree the first time "
        "the API's field-path shape changes."
    )
