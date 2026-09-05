"""Run an ``apps/web`` tool (npm, npx) from the repository root.

pre-commit executes hooks from the repository root, but the frontend tooling
(eslint, prettier, tsc, vitest) is installed in ``apps/web/node_modules`` and
configured relative to ``apps/web``. This changes directory first and rewrites
the file paths pre-commit hands over so they resolve from there.

    python scripts/hooks/run_in_web.py npx prettier --write <files>
    python scripts/hooks/run_in_web.py npm run typecheck
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "apps" / "web"

#: pre-commit hands over paths relative to the repository root; the frontend
#: tooling runs from apps/web, so this prefix is stripped from every argument.
WEB_PREFIX = "apps/web/"


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: run_in_web.py <executable> [args...]\n")
        return 2
    if not (WEB_DIR / "node_modules").exists():
        sys.stderr.write(
            "error: apps/web/node_modules not found. Install first:\n"
            "    make install        (Windows: .\\make.ps1 install)\n"
        )
        return 1
    executable, *args = argv
    resolved = shutil.which(executable)
    if resolved is None:
        sys.stderr.write(f"error: {executable!r} is not on PATH\n")
        return 1
    rewritten = [arg[len(WEB_PREFIX) :] if arg.startswith(WEB_PREFIX) else arg for arg in args]
    completed = subprocess.run(  # noqa: S603 - arguments come from the hook definition
        [resolved, *rewritten], cwd=WEB_DIR, check=False
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
