"""Check Bash syntax without executing scripts, including from PowerShell.

Windows can resolve ``bash`` to an unconfigured WSL launcher. Prefer Git Bash
and reject that launcher so the hook has the same behavior in every terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"


def find_bash() -> str:
    """Resolve the syntax checker; a missing tool is a failure, never a skip."""
    if WINDOWS:
        roots = (
            ("PROGRAMFILES", "Git"),
            ("PROGRAMFILES(X86)", "Git"),
            ("LOCALAPPDATA", "Programs/Git"),
            ("LOCALAPPDATA", "Git"),
        )
        for variable, relative in roots:
            if root := os.environ.get(variable):
                candidate = Path(root) / relative / "bin" / "bash.exe"
                if candidate.is_file():
                    return str(candidate)
    executable = shutil.which("bash")
    if executable and not (
        WINDOWS and Path(executable).parent.name.lower() in {"system32", "sysnative"}
    ):
        return executable
    install = "Install Git Bash (Git for Windows)." if WINDOWS else "Install Bash on PATH."
    raise RuntimeError(f"Bash is required for shell syntax checks. {install}")


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: check_shell.py <script.sh> [script.sh ...]\n")
        return 2
    try:
        bash = find_bash()
        status = 0
        for filename in argv:
            script = (REPO_ROOT / filename).resolve(strict=True)
            if not script.is_file():
                raise ValueError(f"Not a script file: {filename}")
            # Bash reads one script; subsequent filenames would be its arguments.
            # Check each file separately, with absolute paths after the option end.
            result = subprocess.run(  # noqa: S603 - fixed syntax-only argument vector
                [bash, "--noprofile", "--norc", "-n", "--", str(script)],
                cwd=REPO_ROOT,
                check=False,
            )
            if result.returncode:
                status = result.returncode
        return status
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"shell-syntax: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
