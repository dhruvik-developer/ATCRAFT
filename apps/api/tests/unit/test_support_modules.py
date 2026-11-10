"""Test modules share helpers through support modules, which report like tests.

A builder or fixture that one test module imports from another turns that test
file into a library: renaming or splitting it breaks suites that never mention
it, and every importer runs its module-level setup. The shared ones live in
``tests/**/*_support.py``, and ``tests/conftest.py`` registers each of those for
pytest's assert rewriting, so an assert that moved out of a test module into a
builder still reports the values it compared.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parents[1]


def support_modules() -> list[str]:
    """Every support module under ``tests/``, as the dotted name it is imported by."""
    return sorted(
        ".".join(path.relative_to(TESTS.parent).with_suffix("").parts)
        for path in TESTS.rglob("*_support.py")
        if not path.name.startswith("test_")
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
