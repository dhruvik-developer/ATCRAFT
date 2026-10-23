"""Workspace cleanup removes reproducible outputs without touching local data."""

from __future__ import annotations

import importlib.util
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit


@pytest.fixture
def cleanup():
    spec = importlib.util.spec_from_file_location(
        "workspace_cleanup", REPO_ROOT / "scripts/clean.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("keep or regenerate", encoding="utf-8")
    return path


def test_known_outputs_are_discovered_and_dry_run_is_read_only(cleanup, tmp_path: Path) -> None:
    outputs = (
        ".pytest_cache/state",
        ".mypy_cache/state",
        ".ruff_cache/state",
        "htmlcov/index.html",
        "apps/api/.pytest_cache/state",
        "apps/api/.mypy_cache/state",
        "apps/api/.ruff_cache/state",
        "apps/api/htmlcov/index.html",
        "apps/api/__pycache__/obsolete.pyc",
        "apps/api/src/core/__pycache__/config.pyc",
        "apps/api/tests/unit/__pycache__/test_example.pyc",
        "apps/api/scripts/__pycache__/seed_local.pyc",
        "apps/api/migrations/versions/__pycache__/baseline.pyc",
        "scripts/hooks/__pycache__/runner.pyc",
        "scripts/__pycache__/clean.pyc",
        "apps/web/dist/index.html",
        "apps/web/.vite/state",
        "tests/e2e/.auth/user.json",
        "tests/e2e/test-results/result.json",
        "tests/e2e/playwright-report/index.html",
        "tests/e2e/readiness-artifacts/trace.zip",
        ".coverage",
        ".coverage.worker1",
        "coverage.xml",
        "apps/api/.coverage",
        "apps/api/.coverage.worker2",
        "apps/api/coverage.xml",
        "apps/web/.eslintcache",
        "apps/web/tsconfig.app.tsbuildinfo",
        "apps/web/tsconfig.node.tsbuildinfo",
        "tests/e2e/ui-sweep-result.json",
        "tests/e2e/tenant-isolation.json",
        "tests/e2e/a11y-light.json",
        "tests/e2e/final-results.json",
        "tests/e2e/verify-mobile.json",
        "tests/e2e/sweep-quality.json",
    )
    files = [_file(tmp_path, relative) for relative in outputs]

    targets = cleanup.discover_targets(tmp_path)
    assert targets == cleanup.clean(tmp_path, dry_run=True)
    assert all(
        any(file == target or target in file.parents for target in targets) for file in files
    )
    assert all(file.is_file() for file in files)
    assert len(targets) == len(outputs)

    assert cleanup.clean(tmp_path) == targets
    assert all(not file.exists() for file in files)
    assert cleanup.clean(tmp_path) == ()


def test_source_dependencies_local_data_and_deliverables_survive(cleanup, tmp_path: Path) -> None:
    protected = (
        ".env",
        ".env.example",
        ".git/config",
        ".venv/lib/__pycache__/library.pyc",
        "node_modules/tool/__pycache__/library.pyc",
        "apps/api/.venv/lib/__pycache__/library.pyc",
        "apps/api/atcraft_erp_api.egg-info/PKG-INFO",
        "apps/web/node_modules/.vite/deps/module.js",
        "storage/.coverage",
        "storage/__pycache__/business.pyc",
        "apps/api/storage/customer.pdf",
        "backups/atcraft.dump",
        "apps/api/src/core/config.py",
        "apps/api/migrations/versions/baseline.py",
        "apps/api/src/storage/__pycache__/business.pyc",
        "apps/api/src/.venv/__pycache__/library.pyc",
        "apps/api/src/node_modules/__pycache__/library.pyc",
        "apps/api/src/backups/__pycache__/business.pyc",
        "apps/api/src/.git/__pycache__/business.pyc",
        "tests/e2e/package-lock.json",
        "tests/e2e/custom.json",
        "tests/e2e/scripts/a11y-source.json",
        "docs/handbook/Atcraft-ERP-Handbook.pdf",
        "docs/handbook/handbook.html",
        "docs/handbook/screens/login.png",
    )
    files = [
        _file(tmp_path, relative) for relative in protected if not relative.startswith(".git/")
    ]
    _file(tmp_path, ".pytest_cache/cache")

    cleanup.clean(tmp_path)

    assert all(file.is_file() for file in files)


def test_tracked_files_and_their_generated_parent_are_preserved(
    cleanup, tmp_path: Path, monkeypatch
):
    (tmp_path / ".git").mkdir()
    tracked = _file(tmp_path, "tests/e2e/a11y-config.json")
    tracked_child = _file(tmp_path, "apps/web/dist/keep.txt")
    untracked_sibling = _file(tmp_path, "apps/web/dist/generated.js")
    output = _file(tmp_path, "tests/e2e/a11y-results.json")
    monkeypatch.setattr(
        cleanup.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=b"tests/e2e/a11y-config.json\0apps/web/dist/keep.txt\0"
        ),
    )

    cleanup.clean(tmp_path)

    assert tracked.is_file() and tracked_child.is_file() and untracked_sibling.is_file()
    assert not output.exists()


def test_git_failure_stops_cleanup_before_deletion(cleanup, tmp_path: Path, monkeypatch):
    (tmp_path / ".git").mkdir()
    output = _file(tmp_path, ".pytest_cache/state")

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git"])

    monkeypatch.setattr(cleanup.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        cleanup.clean(tmp_path)
    assert output.is_file()


@pytest.mark.parametrize("relative", ("../outside", "../workspace-other/.pytest_cache", "."))
def test_invalid_target_containment_is_rejected(cleanup, tmp_path: Path, relative: str):
    with pytest.raises(ValueError):
        cleanup._validate_path(tmp_path, tmp_path / relative)


@pytest.mark.parametrize(
    "relative",
    ("apps", "apps/api", "apps/api/src", "apps/api/src/__pycache__", "apps/web/dist"),
)
def test_linked_target_or_ancestor_is_rejected(cleanup, tmp_path: Path, monkeypatch, relative: str):
    output = _file(tmp_path, ".pytest_cache/state")
    linked = tmp_path / relative
    linked.mkdir(parents=True, exist_ok=True)
    actual = cleanup._is_link
    monkeypatch.setattr(cleanup, "_is_link", lambda path: path == linked or actual(path))

    with pytest.raises(ValueError, match=r"link|reparse"):
        cleanup.clean(tmp_path)
    assert output.is_file()


def test_nested_links_are_rejected_before_any_output_is_removed(
    cleanup, tmp_path: Path, monkeypatch
):
    first = _file(tmp_path, ".pytest_cache/state")
    linked = _file(tmp_path, "apps/web/dist/assets/external.js")
    actual = cleanup._is_link
    monkeypatch.setattr(cleanup, "_is_link", lambda path: path == linked or actual(path))

    with pytest.raises(ValueError, match="containing a link"):
        cleanup.clean(tmp_path)
    assert first.is_file() and linked.is_file()


def test_source_walk_never_descends_through_links(cleanup, tmp_path: Path, monkeypatch):
    source = _file(tmp_path, "apps/api/src/external/__pycache__/outside.pyc")
    linked_directory = source.parent.parent
    actual = cleanup._is_link
    monkeypatch.setattr(cleanup, "_is_link", lambda path: path == linked_directory or actual(path))

    assert cleanup.clean(tmp_path) == ()
    assert source.is_file()


def test_linked_workspace_ancestor_is_rejected(cleanup, tmp_path: Path, monkeypatch):
    output = _file(tmp_path, ".pytest_cache/state")
    actual = cleanup._is_link
    monkeypatch.setattr(cleanup, "_is_link", lambda path: path == tmp_path.parent or actual(path))

    with pytest.raises(ValueError, match=r"link|reparse"):
        cleanup.clean(tmp_path)
    assert output.is_file()


@pytest.mark.parametrize("mode,attributes", ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)))
def test_posix_links_and_windows_reparse_points_are_detected(
    cleanup, monkeypatch, mode, attributes
):
    monkeypatch.setattr(
        Path, "lstat", lambda self: SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
    )
    assert cleanup._is_link(Path("link"))


@pytest.mark.parametrize("relative", ("apps/web/dist", "apps/api/.coverage"))
def test_unexpected_output_types_stop_cleanup(cleanup, tmp_path: Path, relative: str):
    target = tmp_path / relative
    if relative.endswith("dist"):
        _file(tmp_path, relative)
    else:
        target.mkdir(parents=True)
    with pytest.raises(ValueError, match="Unexpected generated output type"):
        cleanup.clean(tmp_path)
    assert target.exists()


def test_cli_lists_outputs_without_mutation_and_reports_safety_failures(
    cleanup, tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(cleanup, "__file__", str(tmp_path / "scripts/clean.py"))
    output = _file(tmp_path, "apps/api/.coverage")

    assert cleanup.main(["--dry-run"]) == 0
    assert "Would remove" in capsys.readouterr().out
    assert output.is_file()

    assert cleanup.main([]) == 0
    assert "Removed 1 generated output(s)." in capsys.readouterr().out
    assert not output.exists()

    (tmp_path / "apps/api/.coverage").mkdir()
    assert cleanup.main([]) == 1
    assert "Cleanup stopped:" in capsys.readouterr().out
