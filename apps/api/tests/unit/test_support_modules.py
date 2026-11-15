"""Test modules share helpers through support modules, never through each other.

A builder or fixture that one test module imports from another turns that test
file into a library: renaming or splitting it breaks suites that never mention
it, every importer runs its module-level setup, and a fixture comes along only
under the ``name as name`` spelling that re-registers it. Thirty-odd suites
had grown that way, around the procurement, sales-invoice, job-work and MRP
tests. The shared ones live in ``tests/**/*_support.py`` now, and this file
refuses an import of any test module from anywhere under ``tests/``.

``tests/conftest.py`` registers every support module for pytest's assert
rewriting, so an assert that moved out of a test module into a builder still
reports the values it compared; the second half of this file pins that.

Both rules are functions over the tree, each with a self-test that feeds it the
violation it exists to catch.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parents[1]


def _python_files() -> list[Path]:
    return sorted(path for path in TESTS.rglob("*.py") if "__pycache__" not in path.parts)


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(TESTS.parent).with_suffix("").parts)


def imported_test_modules(source: str, *, package: str = "tests") -> list[tuple[int, str]]:
    """``(line, module)`` for each import of a test module in ``source``.

    A test module is a ``test_*`` module anywhere under ``tests``, however the
    import names it: ``from tests.unit.test_x import y``, ``import
    tests.api.test_x``, ``from tests.integration import test_x``, or relatively
    from within ``package``.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*anchor, base] if base else anchor)
            parts = base.split(".")
            if parts[0] != "tests":
                continue
            if parts[-1].startswith("test_"):
                found.append((node.lineno, base))
            else:
                # ``from tests.integration import test_x`` names the module itself.
                found.extend(
                    (node.lineno, f"{base}.{alias.name}")
                    for alias in node.names
                    if alias.name.startswith("test_")
                )
        elif isinstance(node, ast.Import):
            found.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name.split(".")[0] == "tests"
                and any(part.startswith("test_") for part in alias.name.split("."))
            )
    return found


def violations() -> list[str]:
    """Every import of a test module across ``tests/``.

    The frontend guards were the last family let through: they shared their
    source readers by importing one another, until those moved into
    ``tests/frontend_scan_support.py``.
    """
    refused: list[str] = []
    for path in _python_files():
        package = _dotted(path).rpartition(".")[0]
        for line, imported in imported_test_modules(
            path.read_text(encoding="utf-8"), package=package
        ):
            refused.append(f"{path.relative_to(TESTS.parent).as_posix()}:{line} imports {imported}")
    return refused


# -----------------------------------------------------------------------------
# No test module is another's library
# -----------------------------------------------------------------------------


def test_the_scanner_finds_every_way_of_importing_a_test_module() -> None:
    source = (
        "from tests.integration.test_procurement_service import make_order\n"
        "from tests.integration.test_procurement_service import db as db\n"
        "import tests.unit.test_mrp_engine_merge\n"
        "import tests.api.test_vendor_portal_http as portal\n"
        "from tests.integration import test_sales_invoice_service\n"
        "from .test_packing_final_serials import decide\n"
        "from . import test_assembly_completion\n"
        "from tests.unit.test_frontend_plurals import test_helper\n"
    )
    assert imported_test_modules(source, package="tests.integration") == [
        (1, "tests.integration.test_procurement_service"),
        (2, "tests.integration.test_procurement_service"),
        (3, "tests.unit.test_mrp_engine_merge"),
        (4, "tests.api.test_vendor_portal_http"),
        (5, "tests.integration.test_sales_invoice_service"),
        (6, "tests.integration.test_packing_final_serials"),
        (7, "tests.integration.test_assembly_completion"),
        (8, "tests.unit.test_frontend_plurals"),
    ]


def test_the_scanner_lets_support_modules_and_conftest_through() -> None:
    source = (
        "from tests import seed_support\n"
        "from tests.procurement_support import make_order\n"
        "from tests.api.vendor_portal_support import world as world\n"
        "from tests.schema_support import prepare_metadata\n"
        "from tests.conftest import TEST_DB_NAME\n"
        "from src.modules.mrp import engine\n"
        "import pytest\n"
    )
    assert imported_test_modules(source, package="tests.api") == []


def test_no_test_module_imports_another() -> None:
    refused = violations()
    assert not refused, (
        "A test module is imported as a library. Move what is shared into a support "
        "module beside its peers (tests/<topic>_support.py), import it from there in "
        "the original file and in every importer - a fixture with the explicit "
        "`name as name` re-export - and keep its behaviour unchanged:\n  " + "\n  ".join(refused)
    )


# -----------------------------------------------------------------------------
# Support modules report a failing assert as a test does
# -----------------------------------------------------------------------------


def support_modules() -> list[str]:
    """Every support module under ``tests/``, as the dotted name it is imported by."""
    return sorted(
        _dotted(path) for path in TESTS.rglob("*_support.py") if not path.name.startswith("test_")
    )


def rewritten(module: ModuleType) -> bool:
    """Whether pytest's assert rewriting loaded ``module``."""
    return type(module.__loader__).__name__ == "AssertionRewritingHook"


def test_the_check_tells_a_plain_module_from_a_rewritten_one() -> None:
    # This file is a collected test module; tests/unit/__init__.py is neither
    # collected nor registered, so the plain import machinery loaded it.
    assert rewritten(sys.modules[__name__])
    assert not rewritten(importlib.import_module("tests.unit"))


def test_the_support_modules_are_found_at_every_depth() -> None:
    # An empty parameter set below would pass by testing nothing.
    assert {
        "tests.schema_support",
        "tests.seed_support",
        "tests.api.vendor_portal_support",
    } <= set(support_modules())


@pytest.mark.parametrize("name", support_modules())
def test_a_support_module_reports_a_failing_assert_as_a_test_does(name: str) -> None:
    assert rewritten(importlib.import_module(name)), (
        f"{name} was not loaded through pytest's assert rewriting. tests/conftest.py "
        "registers every tests/**/*_support.py before anything imports one; a support "
        "module imported earlier, or named without the suffix, fails its asserts with "
        "a bare AssertionError"
    )
