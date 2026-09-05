"""Run a backend tool with the ``apps/api/.venv`` interpreter, from any platform.

pre-commit hooks declared ``language: system`` execute whatever is on PATH, and
the backend tooling (ruff, mypy, pytest) is pinned in ``apps/api/.venv`` rather
than installed globally - so a hook that ran a bare ``mypy`` would either fail
to find it or, worse, run a different version from the one CI checks against.

This locates the venv interpreter (``Scripts/python.exe`` on Windows,
``bin/python`` elsewhere), changes into ``apps/api`` and runs the requested
module there. It is deliberately dependency-free: it runs before anything else.

    python scripts/hooks/run_in_venv.py ruff check src tests
    python scripts/hooks/run_in_venv.py pytest tests/unit -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "apps" / "api"
VENV_DIR = API_DIR / ".venv"

#: pre-commit hands over paths relative to the repository root; the backend
#: tooling runs from apps/api, so this prefix is stripped from every argument.
API_PREFIX = "apps/api/"


def relocate(arg: str) -> str:
    """Rewrite a repository-relative path so it resolves from ``apps/api``.

    ``apps/api/src/x.py`` becomes ``src/x.py``; a file elsewhere in the
    repository (``scripts/hooks/x.py``) becomes ``../../scripts/hooks/x.py``.
    Anything that is not an existing path - a flag, a module name - is passed
    through untouched.
    """
    if arg.startswith(API_PREFIX):
        return arg[len(API_PREFIX) :]
    if not arg.startswith("-") and (REPO_ROOT / arg).exists():
        return str(Path("..", "..", arg))
    return arg


def venv_python() -> Path:
    for candidate in (VENV_DIR / "Scripts" / "python.exe", VENV_DIR / "bin" / "python"):
        if candidate.exists():
            return candidate
    sys.stderr.write(
        "error: apps/api/.venv not found. Create it first:\n"
        "    make install        (Windows: .\\make.ps1 install)\n"
    )
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: run_in_venv.py <module> [args...]\n")
        return 2
    module, *args = argv
    rewritten = [relocate(arg) for arg in args]
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    completed = subprocess.run(  # noqa: S603 - arguments come from the hook definition
        [str(venv_python()), "-m", module, *rewritten],
        cwd=API_DIR,
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
