"""Install a hash-locked requirements file on any developer platform.

``requirements*.lock`` are produced by ``pip-compile`` on Linux - the platform
CI verifies them on and the platform the container images are built on - and
pip-compile resolves environment markers for the platform it runs on. So the
lock pins ``uvloop`` unconditionally, and ``pip install --require-hashes`` on
Windows stops dead with "uvloop does not support Windows at the moment" before
installing anything at all. The Windows quick start in README.md could not
succeed.

Regenerating the lock on Windows is not the answer: pip-compile would then omit
``uvloop`` altogether, the CI job that regenerates the lock on Linux and diffs
it would fail, and the image would lose its event loop.

This keeps one lock and one source of truth. It copies the lock, drops the
pins this platform cannot use, and installs the rest with ``--require-hashes``
intact - every package that *is* installed is still the exact reviewed
artefact. On Linux and macOS nothing is dropped and the result is identical to
installing the lock directly.

    python -m scripts.install_requirements requirements-dev.lock
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1]

#: Distributions with no build for the platform, keyed by ``sys.platform``.
#: uvloop is Unix-only by design; uvicorn falls back to asyncio's loop without it.
PLATFORM_UNSUPPORTED: dict[str, frozenset[str]] = {
    "win32": frozenset({"uvloop"}),
    "cygwin": frozenset({"uvloop"}),
}


def _entries(text: str) -> list[list[str]]:
    """Split a pip-compile lock into per-requirement blocks.

    A block is the ``name==version \\`` line plus its indented ``--hash`` and
    ``# via`` continuation lines; a blank line ends nothing (pip-compile does
    not emit them between entries) but is kept with whatever precedes it.
    """
    blocks: list[list[str]] = []
    for line in text.splitlines(keepends=True):
        continuation = line[:1].isspace() or line.startswith("#") or not line.strip()
        if continuation and blocks:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    return blocks


def _name(block: list[str]) -> str:
    head = block[0].split("==", 1)[0].strip().lower()
    return head.split("[", 1)[0]


def filter_lock(text: str, unsupported: frozenset[str]) -> tuple[str, list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for block in _entries(text):
        name = _name(block)
        if name in unsupported:
            dropped.append(name)
            continue
        kept.extend(block)
    return "".join(kept), dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("lock", type=Path, help="requirements.lock or requirements-dev.lock")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interpreter whose environment receives the install (default: this one).",
    )
    args = parser.parse_args(argv)

    lock_path = args.lock if args.lock.is_absolute() else API_DIR / args.lock
    unsupported = PLATFORM_UNSUPPORTED.get(sys.platform, frozenset())
    filtered, dropped = filter_lock(lock_path.read_text(encoding="utf-8"), unsupported)
    for name in dropped:
        print(f"skipping {name}: no build for {sys.platform}")

    with tempfile.NamedTemporaryFile(
        "w", suffix=".lock", prefix="atcraft-", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(filtered)
        temp_path = Path(handle.name)
    try:
        command = [args.python, "-m", "pip", "install", "--require-hashes", "-r", str(temp_path)]
        return subprocess.run(command, cwd=API_DIR, check=False).returncode  # noqa: S603
    finally:
        temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
