"""A refusal raised from two modules is worded once, in ``core/messages.py``.

CLAUDE.md (Conventions): "backend ``src/core/messages.py`` for any sentence
raised from two or more modules". Twenty-nine sentences broke that and shipped -
the session-revoked refusal in ``core.deps`` and ``auth``, the priced-line rules
in procurement and sales, the hierarchy rules in organisation and warehouses,
"must be cancelled rather than deleted" in assembly, job work and sales. When a
sentence is reworded, or translated, every copy has to be found by hand, and
the priced-line rules could already drift apart between the two documents.

The rule is a function over source text and has a self-test that feeds it the
violation it exists to catch. A message counts as a sentence when its literal
text holds at least three words; ``core`` and ``common`` count as modules.
Only literals and f-strings are compared - a message built by a
``core.messages`` function is a call, so the honest code cannot trip it.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from src.core import messages
from tests.message_scan_support import _error_classes, _template

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

_MIN_WORDS = 3


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _module(relative: str) -> str:
    parts = relative.split("/")
    return parts[1] if parts[0] == "modules" and len(parts) > 2 else parts[0]


def raised_sentences(source: str, *, label_classes: set[str]) -> list[str]:
    """Every literal refusal ``source`` raises, placeholders written ``{}``."""
    sentences: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        raised = node.exc.func
        name = raised.id if isinstance(raised, ast.Name) else getattr(raised, "attr", "")
        if name in label_classes or not node.exc.args:
            continue
        text = _template(node.exc.args[0])
        if text is not None and len(text.replace("{}", "").split()) >= _MIN_WORDS:
            sentences.append(text.strip())
    return sentences


def shared_sentences(sources: dict[str, str], *, label_classes: set[str]) -> dict[str, list[str]]:
    """``sentence -> modules`` for each sentence two or more modules raise themselves."""
    owners: dict[str, set[str]] = defaultdict(set)
    for relative, source in sources.items():
        for sentence in raised_sentences(source, label_classes=label_classes):
            owners[sentence].add(_module(relative))
    return {sentence: sorted(found) for sentence, found in owners.items() if len(found) > 1}


def test_no_sentence_is_raised_from_two_modules() -> None:
    files = _python_files()
    sources = {path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8") for path in files}
    _, label_classes = _error_classes([ast.parse(source) for source in sources.values()])

    shared = shared_sentences(sources, label_classes=label_classes)

    assert not shared, (
        f"{len(shared)} sentence(s) are written out in more than one module:\n"
        + "\n".join(f"  {sentence!r}: {', '.join(found)}" for sentence, found in shared.items())
        + "\n\nMove each into src/core/messages.py - a constant, or a function for a "
        "sentence with values - and raise it from there."
    )


def test_the_rule_flags_a_sentence_two_modules_write_out() -> None:
    sources = {
        "core/deps.py": "raise AuthenticationError('Session has been revoked.', code=c)",
        "modules/auth/service.py": "\n".join(
            [
                "raise AuthenticationError('Session has been revoked.', code=c)",
                "raise BusinessRuleError(f'Sales order {code} belongs to a different customer.')",
                "raise NotFoundError('Purchase order line', line_id)",
            ]
        ),
        "modules/packing/service.py": "\n".join(
            [
                "raise BusinessRuleError(f'Sales order {h[0]} belongs to a different customer.')",
                "raise NotFoundError('Purchase order line', line_id)",
                "raise ValidationError(SESSION_REVOKED)",
                "raise ValueError('Too short.')",
            ]
        ),
        "modules/packing/rules.py": "raise ValueError('Every carton is sealed once.')",
        "modules/packing/service_two.py": "raise ValueError('Every carton is sealed once.')",
    }

    shared = shared_sentences(sources, label_classes={"NotFoundError"})

    # One module raising a sentence from two files is its own business; two are not.
    assert shared == {
        "Session has been revoked.": ["auth", "core"],
        "Sales order {} belongs to a different customer.": ["auth", "packing"],
    }


# --- the shared builders render the sentences the modules used to write out -----------


def test_the_shared_builders_keep_each_module_s_wording() -> None:
    assert messages.inactive_record("Customer", "C-001", "cannot be dispatched to") == (
        "Customer C-001 is inactive and cannot be dispatched to."
    )
    assert messages.date_before("required date", "order date") == (
        "The required date cannot be before the order date."
    )
    assert messages.own_parent("department") == "A department cannot be its own parent."
    assert messages.parent_below("storage location") == (
        "That parent is already below this storage location; the hierarchy must stay a tree."
    )
    assert messages.hierarchy_too_deep("department", 8) == (
        "Department hierarchy may not be deeper than 8 levels."
    )
    assert messages.no_price_configured("SALE", "HNG-1") == (
        "No SALE price is configured for item HNG-1. Add a price list entry for this channel."
    )
    assert messages.line_not_serial_tracked(3, "HNG-1") == (
        "Line 3: HNG-1 is not serial tracked; remove the serial numbers."
    )
    assert messages.output_not_serial_tracked("HNG-1") == (
        "HNG-1 is not serial tracked; remove the output serials."
    )
    assert messages.other_customer_order("SO-7") == (
        "Sales order SO-7 belongs to a different customer."
    )
    assert messages.cancel_not_delete("JW-2", "ISSUED") == (
        "JW-2 is ISSUED and must be cancelled rather than deleted, so the record of it survives."
    )
    assert messages.obsolete_not_delete("BOM-4", "APPROVED") == (
        "BOM-4 is APPROVED. Only a draft can be deleted; make it obsolete instead."
    )
    assert messages.draft_only_delete("PK-9", "PACKING") == (
        "PK-9 is PACKING; only a draft may be deleted. Cancel it instead."
    )
    assert messages.channel_not_enabled("WHATSAPP") == "WHATSAPP notifications are not enabled."
