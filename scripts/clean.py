"""Remove reproducible workspace outputs while preserving source and local data.

The allowlist deliberately excludes dependencies, runtime storage, backups and
handbook deliverables. Run ``python scripts/clean.py --dry-run`` to review it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_BYTECODE_ROOTS = (
    "apps/api/src",
    "apps/api/tests",
    "apps/api/scripts",
    "apps/api/migrations",
    "scripts/hooks",
)
_DIRECTORIES = (
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    "apps/api/.mypy_cache",
    "apps/api/.pytest_cache",
    "apps/api/.ruff_cache",
    "apps/api/htmlcov",
    "apps/api/__pycache__",
    "scripts/__pycache__",
    "apps/web/dist",
    "apps/web/.vite",
    "tests/e2e/.auth",
    "tests/e2e/test-results",
    "tests/e2e/playwright-report",
    "tests/e2e/readiness-artifacts",
)
_FILES = (
    ".coverage",
    "coverage.xml",
    "apps/api/.coverage",
    "apps/api/coverage.xml",
    "apps/web/.eslintcache",
    "apps/web/tsconfig.app.tsbuildinfo",
    "apps/web/tsconfig.node.tsbuildinfo",
)
# These are the output patterns documented in tests/e2e/.gitignore. They only
# apply to that directory itself; fixtures, scripts and arbitrary JSON survive.
_E2E_OUTPUTS = (
    "*-sweep*.json",
    "tenant-isolation.json",
    "a11y*.json",
    "final*.json",
    "verify-*.json",
    "sweep*.json",
)
_PROTECTED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "storage", "backups"})


def _is_link(path: Path) -> bool:
    attributes = path.lstat()
    return stat.S_ISLNK(attributes.st_mode) or bool(
        getattr(attributes, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _workspace(root: Path) -> Path:
    root = Path(os.path.abspath(root))
    for ancestor in (*reversed(root.parents), root):
        if _is_link(ancestor):
            raise ValueError(f"Refusing cleanup through a link or reparse point: {ancestor}")
    if not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {root}")
    return root


def _validate_path(root: Path, target: Path) -> None:
    """Check lexical containment before resolving, so links cannot hide escapes."""
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Cleanup target is outside the workspace: {target}") from exc
    if not relative.parts or ".." in relative.parts:
        raise ValueError(f"Invalid cleanup target: {target}")
    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current):
            if _is_link(current):
                raise ValueError(f"Refusing cleanup through a link or reparse point: {current}")
        else:
            break
    if not target.resolve().is_relative_to(root):
        raise ValueError(f"Cleanup target resolves outside the workspace: {target}")


def _validate_tree(root: Path, target: Path) -> None:
    _validate_path(root, target)
    if target.is_dir():
        for directory, names, files in os.walk(target, followlinks=False):
            for name in (*names, *files):
                child = Path(directory) / name
                if _is_link(child):
                    raise ValueError(f"Refusing cleanup of a tree containing a link: {child}")


def _tracked_paths(root: Path) -> set[Path]:
    if not (root / ".git").exists():
        return set()
    # Never delete a tracked file, even if a future commit places it under an
    # output pattern. Git is already required to work in this repository.
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "-z"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return {root / name for name in os.fsdecode(result.stdout).split("\0") if name}


def discover_targets(root: Path) -> tuple[Path, ...]:
    """Find only known generated outputs, without following directory links."""
    root = _workspace(root)
    targets: set[Path] = set()
    for entries, is_directory in ((_DIRECTORIES, True), (_FILES, False)):
        for relative in entries:
            target = root / relative
            _validate_path(root, target)
            if target.exists():
                if target.is_dir() != is_directory:
                    raise ValueError(f"Unexpected generated output type: {target}")
                targets.add(target)
    for directory, patterns in (
        (root, (".coverage.*",)),
        (root / "apps/api", (".coverage.*",)),
        (root / "tests/e2e", _E2E_OUTPUTS),
    ):
        if directory != root:
            _validate_path(root, directory)
        for pattern in patterns:
            for target in directory.glob(pattern):
                _validate_path(root, target)
                if target.is_file():
                    targets.add(target)
    for relative in _BYTECODE_ROOTS:
        source = root / relative
        _validate_path(root, source)
        if not source.is_dir():
            continue
        for directory, names, _ in os.walk(source, followlinks=False):
            descend = []
            for name in names:
                child = Path(directory) / name
                if name == "__pycache__":
                    _validate_path(root, child)
                    targets.add(child)
                elif name not in _PROTECTED_DIRECTORIES and not _is_link(child):
                    descend.append(name)
            names[:] = descend
    tracked = _tracked_paths(root)
    removable = tuple(
        target
        for target in sorted(targets)
        if not any(path == target or target in path.parents for path in tracked)
    )
    for target in removable:
        _validate_tree(root, target)
    return removable


def clean(root: Path, *, dry_run: bool = False) -> tuple[Path, ...]:
    """Preflight every target, then remove it; repeated calls are harmless."""
    root = _workspace(root)
    targets = discover_targets(root)
    if not dry_run:
        for target in targets:
            _validate_tree(root, target)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
    return targets


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="List outputs without removing them."
    )
    options = parser.parse_args(arguments)
    root = Path(__file__).resolve().parents[1]
    try:
        targets = clean(root, dry_run=options.dry_run)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.stdout.write(f"Cleanup stopped: {exc}\n")
        return 1
    for target in targets:
        sys.stdout.write(
            f"{'Would remove' if options.dry_run else 'Removed'} {target.relative_to(root)}\n"
        )
    sys.stdout.write(
        f"{'Found' if options.dry_run else 'Removed'} {len(targets)} generated output(s).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
