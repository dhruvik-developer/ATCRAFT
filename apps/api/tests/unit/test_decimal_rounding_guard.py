"""Every ``Decimal.quantize`` in ``src`` names its rounding mode.

``quantize`` without ``rounding=`` takes the context default, ROUND_HALF_EVEN.
Money, prices and quantities here round half-up (``src.core.types``: HALF_UP is
what Indian GST invoicing expects), so a bare ``quantize`` at one of those scales
disagrees with ``to_money`` / ``to_unit_price`` on every exact tie. Nothing
errors and the drift is a millionth per unit, so review alone missed it: the
goods-receipt unit cost - which becomes the FIFO layer cost - and the RFQ
comparison price both rounded half-even until November 2026.

Use the ``src.core.types`` helpers, or pass ``rounding=`` yourself. The few bare
calls that round a ratio rather than a stored amount are listed below with the
reason, keyed by file and enclosing function, so a new one is a decision someone
has to write down. The rule is a function over source text, with a self-test
that feeds it the violation it exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: ``"<file under src>::<enclosing function>"`` -> why its bare quantize is not money.
BARE_QUANTIZE_ALLOWED: dict[str, str] = {
    "core/demo_data/operations.py::_measurement": (
        "a demo QC reading at the measured-value scale, generated, not money"
    ),
    "modules/job_work/service.py::loss_percentage": "a scrap percentage, not an amount",
    "modules/procurement/inbound_service.py::tracking_read": "a received percentage for display",
    "modules/procurement/service_rules.py::_ratio": "a quotation-ranking score ratio",
    "modules/procurement/service_rules.py::score_offer": "a weighted quotation-ranking score",
    "modules/quality/service.py::rejection_rate": "a rejection percentage, not an amount",
}


class _BareQuantizeFinder(ast.NodeVisitor):
    """Collects ``(enclosing function, line)`` for each bare ``.quantize`` call."""

    def __init__(self) -> None:
        self.scope = ["<module>"]
        self.found: list[tuple[str, int]] = []

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "quantize"
            # The rounding mode is quantize's second positional parameter; a
            # ``**kwargs`` splat (arg is None) may carry it, so it is not flagged.
            and len(node.args) < 2
            and not any(keyword.arg in {"rounding", None} for keyword in node.keywords)
        ):
            self.found.append((self.scope[-1], node.lineno))
        self.generic_visit(node)


def bare_quantize_calls(source: str) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` of every quantize that leaves rounding to the context."""
    finder = _BareQuantizeFinder()
    finder.visit(ast.parse(source))
    return sorted(finder.found, key=lambda found: found[1])


def _python_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _bare_calls_by_key() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _python_files():
        relative = path.relative_to(SRC).as_posix()
        for function, line in bare_quantize_calls(path.read_text(encoding="utf-8")):
            found.setdefault(f"{relative}::{function}", []).append(line)
    return found


def test_the_scan_finds_the_code_it_polices() -> None:
    assert len(_python_files()) >= 200, "source discovery found too few files"


def test_every_quantize_names_its_rounding_mode() -> None:
    offenders = {
        key: lines
        for key, lines in _bare_calls_by_key().items()
        if key not in BARE_QUANTIZE_ALLOWED
    }
    assert not offenders, (
        "Round money, prices and quantities with src.core.types (to_money, "
        "to_unit_price, to_quantity), or pass rounding= explicitly. If this value is "
        "a ratio and not an amount, add it to BARE_QUANTIZE_ALLOWED with the reason: "
        f"{offenders}"
    )


def test_every_bare_quantize_allowance_is_still_needed() -> None:
    present = _bare_calls_by_key()
    stale = [key for key in BARE_QUANTIZE_ALLOWED if key not in present]
    assert not stale, f"remove these BARE_QUANTIZE_ALLOWED entries, the bare call is gone: {stale}"


# -- the rule rejects what it exists to catch ---------------------------------


def test_the_rule_rejects_a_quantize_that_leaves_rounding_to_the_context() -> None:
    source = (
        "from decimal import ROUND_HALF_UP, Decimal\n"
        "Q = Decimal('0.000001')\n"
        "def unit_cost(amount, quantity):\n"
        "    return (amount / quantity).quantize(Q)\n"
        "async def kept(amount):\n"
        "    a = amount.quantize(Q, rounding=ROUND_HALF_UP)\n"
        "    b = amount.quantize(Q, ROUND_HALF_UP)\n"
        "    return a, b\n"
        "TOTAL = Decimal('1').quantize(Q)\n"
    )
    assert bare_quantize_calls(source) == [("unit_cost", 4), ("<module>", 9)]
