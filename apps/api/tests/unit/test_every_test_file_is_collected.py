"""Every Python test file in the repository is collected by CI or a hook.

``tests/load/test_locustfile.py`` and ``test_make_write_fixtures.py`` - 36 tests
of the load harness's account and write-fixture validation - sat outside every
pytest invocation: pytest's ``testpaths`` is ``apps/api/tests``, CI ran
``pytest tests/unit`` and ``pytest tests`` from ``apps/api``, and the pre-push
hook ran ``pytest tests/unit``. They passed when someone finally ran them, but
nothing would have noticed the day they stopped. A test no gate runs is the
"never-executed test" trap in CLAUDE.md.

This reads the pytest commands CI and the hooks actually run, works out the
directories they collect, and requires every ``test_*.py`` / ``*_test.py`` file
to sit under one of them.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import tomllib
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml

from src.core.config import API_ROOT, REPO_ROOT

pytestmark = pytest.mark.unit

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
HOOK_RUNNER = REPO_ROOT / "scripts" / "hooks" / "run_in_venv.py"

#: Directories that hold no test of ours: dependencies, caches, build output,
#: runtime data and the frontend, which has no Python.
PRUNED = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "dist",
        "build",
        "storage",
        "backups",
        "test-results",
        "playwright-report",
    }
)
#: pytest options that take the next token as their value.
OPTIONS_WITH_VALUE = frozenset(
    {"-p", "-o", "-k", "-m", "-c", "--rootdir", "--confcutdir", "--basetemp", "--cov-config"}
)


def python_test_files(root: Path) -> list[Path]:
    """Every file pytest's default patterns would treat as a test module."""
    found = []
    for directory, subdirectories, files in os.walk(root):
        here = Path(directory)
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in PRUNED and here / name != root / "apps" / "web"
        ]
        found += [
            here / name
            for name in files
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
        ]
    return sorted(found)


def collected_directories(command: str, working_directory: Path) -> list[Path]:
    """What one pytest command collects, run from ``working_directory``."""
    tokens = shlex.split(command)
    arguments = tokens[tokens.index("pytest") + 1 :]
    paths: list[str] = []
    skip = False
    for token in arguments:
        if skip:
            skip = False
        elif token in OPTIONS_WITH_VALUE:
            skip = True
        elif not token.startswith("-"):
            paths.append(token)
    if not paths:
        # A bare `pytest` collects the testpaths of the config it finds there.
        config = tomllib.loads((working_directory / "pyproject.toml").read_text("utf-8"))
        paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    return [(working_directory / path).resolve() for path in paths]


def ci_invocations(workflow: dict) -> list[tuple[str, Path]]:
    """Every pytest command in the workflow, with the directory it runs from."""
    found = []
    for job in workflow["jobs"].values():
        default = job.get("defaults", {}).get("run", {}).get("working-directory", ".")
        for step in job.get("steps", []):
            directory = REPO_ROOT / step.get("working-directory", default)
            found += [
                (line.strip(), directory)
                for line in str(step.get("run", "")).splitlines()
                if line.strip().startswith("pytest")
            ]
    return found


def _relocate():
    spec = importlib.util.spec_from_file_location("run_in_venv", HOOK_RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.relocate


def hook_invocations(config: dict) -> list[tuple[str, Path]]:
    """Every hook that runs pytest, rewritten as run_in_venv runs it (from apps/api)."""
    relocate = _relocate()
    found = []
    for repository in config["repos"]:
        for hook in repository.get("hooks", []):
            tokens = shlex.split(str(hook.get("entry", "")))
            if "pytest" not in tokens:
                continue
            assert any(token.endswith("run_in_venv.py") for token in tokens), (
                f"hook {hook['id']} runs pytest some other way; teach this test how"
            )
            arguments = [relocate(token) for token in tokens[tokens.index("pytest") + 1 :]]
            found.append((shlex.join(["pytest", *arguments]), API_ROOT))
    return found


def uncollected(files: Iterable[Path], invocations: Iterable[tuple[str, Path]]) -> list[str]:
    roots = [
        directory
        for command, working_directory in invocations
        for directory in collected_directories(command, working_directory)
    ]
    return [
        path.relative_to(REPO_ROOT).as_posix()
        for path in files
        if not any(path.resolve().is_relative_to(root) for root in roots)
    ]


def _invocations() -> list[tuple[str, Path]]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    hooks = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    return ci_invocations(workflow) + hook_invocations(hooks)


def test_every_python_test_file_is_collected_by_ci_or_a_hook() -> None:
    files = python_test_files(REPO_ROOT)
    assert any(path.parent.name == "load" for path in files), "the walk missed tests/load"

    assert uncollected(files, _invocations()) == []


def test_the_load_harness_is_run_by_ci_and_by_the_pre_push_hook() -> None:
    """Both, not either: CI is the gate, and the hook is where a break shows first."""
    load = (REPO_ROOT / "tests" / "load").resolve()
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    hooks = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    for invocations in (ci_invocations(workflow), hook_invocations(hooks)):
        assert any(
            load in collected_directories(command, directory) for command, directory in invocations
        )


def test_the_guard_reports_the_harness_tests_nothing_ran() -> None:
    """Fed the invocations as they were before tests/load was added."""
    shipped = [
        (command, directory)
        for command, directory in _invocations()
        if "tests/load" not in command.replace("\\", "/")
    ]
    assert uncollected(python_test_files(REPO_ROOT), shipped) == [
        "tests/load/test_locustfile.py",
        "tests/load/test_make_write_fixtures.py",
    ]
