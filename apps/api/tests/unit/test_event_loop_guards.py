"""Mechanical guard: a request path never runs known CPU-bound work on the event loop.

Argon2id costs ~37 ms a hash or verify (``src/core/security.py``), and a
reportlab sheet costs ~29 ms a serial label. Either, called straight from an
``async def``, freezes every request on that gunicorn worker for the duration -
health probes included - which is why ``hash_password_async`` and
``verify_password_async`` exist and why documents and renderers go through
``anyio.to_thread.run_sync``. Three administrator password writes and the label
sheet still called the synchronous forms; review did not catch them because the
call reads perfectly naturally.

The rule: inside an ``async def`` under ``src/modules``, a *call* of
``hash_password``, ``verify_password`` or any ``render_*_pdf`` /
``render_*_xlsx`` is refused. Passing the function itself to
``anyio.to_thread.run_sync`` or ``asyncio.to_thread`` is not a call, so the
offloaded form passes. Nested ``def`` and ``lambda`` bodies are not inspected:
they are what gets handed to a thread.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src"

#: Synchronous functions that must not be called on the event loop.
_BLOCKING_NAMES = frozenset({"hash_password", "verify_password"})
_BLOCKING_PATTERN = re.compile(r"^render_\w+_(pdf|xlsx)$")


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_blocking(name: str | None) -> bool:
    return name is not None and (name in _BLOCKING_NAMES or bool(_BLOCKING_PATTERN.match(name)))


def _own_body(function: ast.AsyncFunctionDef) -> list[ast.AST]:
    """Every node of ``function`` except those inside a nested def or lambda."""
    found: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def blocking_calls_in_async(source: str) -> list[str]:
    """``line N: name()`` for each blocking call made directly in an ``async def``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in _own_body(node):
            if isinstance(inner, ast.Call) and _is_blocking(name := _called_name(inner)):
                found.append((inner.lineno, f"line {inner.lineno}: {name}() in {node.name}"))
    return [text for _, text in sorted(found)]


def _module_files() -> list[Path]:
    return sorted(p for p in (SRC / "modules").rglob("*.py") if "__pycache__" not in p.parts)


def test_the_scan_reads_the_modules_it_polices() -> None:
    assert len(_module_files()) >= 100, "module discovery found too few files"


def test_no_async_function_calls_a_blocking_hash_or_renderer() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): found
        for path in _module_files()
        if (found := blocking_calls_in_async(path.read_text(encoding="utf-8")))
    }
    assert not offenders, (
        "Await hash_password_async / verify_password_async from src.core.security, and "
        "hand a renderer to `await anyio.to_thread.run_sync(render, data)`: a direct call "
        f"stalls every request on the worker for its whole duration: {offenders}"
    )


# -- the rule rejects what it exists to catch ----------------------------------


def test_the_rule_rejects_direct_calls_and_accepts_the_offloaded_forms() -> None:
    source = (
        "import anyio, asyncio\n"
        "from src.core import security\n"
        "async def create(payload):\n"
        "    user.password_hash = hash_password(payload.password)\n"
        "    ok = security.verify_password(payload.password, stored)\n"
        "    return render_serial_labels_pdf(reads)\n"
        "async def export(rows):\n"
        "    data = render_report_xlsx(rows)\n"
        "    pdf = await anyio.to_thread.run_sync(render_serial_labels_pdf, reads)\n"
        "    digest = await asyncio.to_thread(hash_password, plain)\n"
        "    work = lambda: render_report_pdf(rows)\n"
        "    def in_a_thread():\n"
        "        return hash_password(plain)\n"
        "    return pdf, digest, work, in_a_thread, data\n"
        "def cli(plain):\n"
        "    return hash_password(plain)\n"
    )
    assert blocking_calls_in_async(source) == [
        "line 4: hash_password() in create",
        "line 5: verify_password() in create",
        "line 6: render_serial_labels_pdf() in create",
        "line 8: render_report_xlsx() in export",
    ]
