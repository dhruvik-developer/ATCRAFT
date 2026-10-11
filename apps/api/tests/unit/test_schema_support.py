"""``database_unreachable`` decides whether a database set-up failure is a skip.

It used to test ``isinstance(exc, OSError | <tuple of asyncpg errors>)``, and a
union with a tuple raises TypeError - so every real set-up failure (a broken
model, a refused constraint drop) surfaced as a TypeError about the helper
instead of the error itself. The cases below pin both halves of its contract.
"""

from __future__ import annotations

import pytest

from tests.schema_support import database_unreachable

pytestmark = pytest.mark.unit


def test_a_refused_connection_is_unreachable() -> None:
    assert database_unreachable(ConnectionRefusedError("refused")) is True


def test_a_wrapped_refusal_is_found_down_the_cause_chain() -> None:
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("could not start the pool") from inner
    except RuntimeError as outer:
        assert database_unreachable(outer) is True


def test_a_real_defect_is_not_mistaken_for_an_outage() -> None:
    """A schema error must re-raise as itself, not be skipped - or crash here."""
    assert database_unreachable(ValueError("constraint does not exist")) is False
