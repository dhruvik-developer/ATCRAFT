"""The cross-platform lock installer keeps every hash it does not drop.

The lock is resolved on Linux, so it pins ``uvloop`` unconditionally and a
plain ``pip install --require-hashes`` on Windows fails before installing
anything. The installer drops the pins the platform cannot build and must
leave everything else - the ``--hash`` lines above all - byte-for-byte intact,
or hash verification would refuse the rest too.
"""

from __future__ import annotations

from scripts.install_requirements import PLATFORM_UNSUPPORTED, filter_lock

LOCK = """\
alembic==1.19.1 \\
    --hash=sha256:aaaa \\
    --hash=sha256:bbbb
    # via atcraft-erp-api (pyproject.toml)
uvloop==0.22.1 \\
    --hash=sha256:cccc
    # via uvicorn
watchfiles==1.2.0 \\
    --hash=sha256:dddd
    # via
    #   atcraft-erp-api (pyproject.toml)
    #   uvicorn
"""


def test_nothing_is_dropped_when_nothing_is_unsupported() -> None:
    kept, dropped = filter_lock(LOCK, frozenset())
    assert kept == LOCK
    assert dropped == []


def test_an_unsupported_pin_is_removed_with_its_hashes_and_via_comment() -> None:
    kept, dropped = filter_lock(LOCK, frozenset({"uvloop"}))
    assert dropped == ["uvloop"]
    assert "uvloop" not in kept
    assert "sha256:cccc" not in kept
    # Neighbours are untouched, continuation lines included.
    assert "alembic==1.19.1 \\\n    --hash=sha256:aaaa \\\n    --hash=sha256:bbbb\n" in kept
    assert "watchfiles==1.2.0 \\\n    --hash=sha256:dddd\n    # via\n" in kept


def test_matching_is_by_distribution_name_and_case_insensitive() -> None:
    kept, dropped = filter_lock(LOCK.replace("uvloop==", "UvLoop=="), frozenset({"uvloop"}))
    assert dropped == ["uvloop"]
    assert "UvLoop" not in kept


def test_windows_skips_exactly_uvloop() -> None:
    # uvicorn falls back to asyncio's loop without it; nothing else in the lock
    # is Unix-only. Extending this set is a decision, not a side effect.
    assert PLATFORM_UNSUPPORTED["win32"] == frozenset({"uvloop"})
    assert PLATFORM_UNSUPPORTED["cygwin"] == frozenset({"uvloop"})
