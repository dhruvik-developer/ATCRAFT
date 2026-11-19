"""Mechanical guard: every cache namespace is read by something.

``CacheNamespace.PERMISSIONS`` was declared with a TTL and "invalidated on role
change", and every user and role write deleted its keys after the commit - one
Redis round trip per affected account, so editing a role held by a hundred
people was a hundred sequential deletes, each waiting out the Redis timeout
while Redis stalled. Nothing ever wrote those keys: the principal is loaded from
the database on every request (``src/core/deps.py``), so there was no permission
cache to invalidate. A namespace nobody reads is a cost with no benefit and a
false statement about how revocation works.

The rule: each namespace declared on ``CacheNamespace`` is referenced from a
module outside ``core/cache.py`` that also reads the cache (``cache_get``).
"""

from __future__ import annotations

import ast
import re

import pytest

from tests.source_scan_support import source_texts

pytestmark = pytest.mark.unit

_CACHE_MODULE = "core/cache.py"


def declared_namespaces(cache_source: str) -> list[str]:
    """The attribute names assigned in the body of ``class CacheNamespace``."""
    for node in ast.parse(cache_source).body:
        if isinstance(node, ast.ClassDef) and node.name == "CacheNamespace":
            return [
                target.id
                for statement in node.body
                if isinstance(statement, ast.Assign)
                for target in statement.targets
                if isinstance(target, ast.Name)
            ]
    return []


def unread_namespaces(names: list[str], sources: dict[str, str]) -> list[str]:
    """Names no cache-reading module outside ``core/cache.py`` refers to."""
    readers = [
        text
        for path, text in sources.items()
        if path != _CACHE_MODULE and re.search(r"\bcache_get\(", text)
    ]
    return [
        name
        for name in names
        if not any(re.search(rf"\bCacheNamespace\.{name}\b", text) for text in readers)
    ]


def test_every_cache_namespace_has_a_reader() -> None:
    sources = source_texts()
    names = declared_namespaces(sources[_CACHE_MODULE])
    assert len(names) >= 3, f"namespace discovery found too few: {names}"
    unread = unread_namespaces(names, sources)
    assert not unread, (
        "Nothing outside core/cache.py reads these namespaces, so every write or "
        f"delete against them is a Redis round trip for nothing - remove them: {unread}"
    )


# -- the rule rejects what it exists to catch ----------------------------------


def test_the_rule_rejects_a_namespace_that_is_only_deleted() -> None:
    cache = (
        "class CacheNamespace:\n"
        '    REPORT = ("report", 300)\n'
        '    PERMISSIONS = ("perm", 300)\n'
        '    UNUSED = ("unused", 60)\n'
    )
    sources = {
        _CACHE_MODULE: cache + "ns, _ = CacheNamespace.UNUSED\nawait cache_get(ns)\n",
        "modules/reports/service.py": "key = CacheNamespace.REPORT\nawait cache_get(key)\n",
        "modules/users/service.py": "ns, _ = CacheNamespace.PERMISSIONS\nawait cache_delete(ns)\n",
    }
    names = declared_namespaces(cache)
    assert names == ["REPORT", "PERMISSIONS", "UNUSED"]
    assert unread_namespaces(names, sources) == ["PERMISSIONS", "UNUSED"]
