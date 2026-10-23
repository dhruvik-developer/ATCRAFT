"""The shell hook checks every file without executing commands or selecting WSL."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location(
        "shell_syntax", REPO_ROOT / "scripts/hooks/check_shell.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_prefers_git_bash_over_the_wsl_launcher(checker, monkeypatch, tmp_path):
    git_bash = tmp_path / "Git/bin/bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.touch()
    monkeypatch.setattr(checker, "WINDOWS", True)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.setattr(checker.shutil, "which", lambda _: "C:/Windows/System32/bash.exe")
    assert checker.find_bash() == str(git_bash)


def test_per_user_git_installation_is_found(checker, monkeypatch, tmp_path):
    git_bash = tmp_path / "Programs/Git/bin/bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.touch()
    monkeypatch.setattr(checker, "WINDOWS", True)
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert checker.find_bash() == str(git_bash)


@pytest.mark.parametrize("executable", [None, "C:/Windows/System32/bash.exe"])
def test_windows_missing_bash_is_an_explicit_failure(checker, monkeypatch, capsys, executable):
    monkeypatch.setattr(checker, "WINDOWS", True)
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(checker.shutil, "which", lambda _: executable)
    assert checker.main(["scripts/deploy.sh"]) == 1
    assert "Install Git Bash" in capsys.readouterr().err


def test_other_platforms_use_path_and_fail_when_bash_is_missing(checker, monkeypatch):
    monkeypatch.setattr(checker, "WINDOWS", False)
    monkeypatch.setattr(checker.shutil, "which", lambda _: "/usr/bin/bash")
    assert checker.find_bash() == "/usr/bin/bash"
    monkeypatch.setattr(checker.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Install Bash on PATH"):
        checker.find_bash()


def test_each_script_gets_a_separate_syntax_only_argument_vector(checker, monkeypatch, tmp_path):
    paths = [tmp_path / "first script.sh", tmp_path / "second.sh"]
    for path in paths:
        path.touch()
    calls = []
    monkeypatch.setattr(checker, "find_bash", lambda: "/tools/bash")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=2 if len(calls) == 1 else 0)

    monkeypatch.setattr(checker.subprocess, "run", run)
    assert checker.main([str(path) for path in paths]) == 2
    assert [args for args, _ in calls] == [
        ["/tools/bash", "--noprofile", "--norc", "-n", "--", str(path)] for path in paths
    ]
    assert all(kwargs == {"cwd": REPO_ROOT, "check": False} for _, kwargs in calls)


def test_syntax_check_detects_later_invalid_files_without_executing_any_script(checker, tmp_path):
    marker = tmp_path / "must-not-exist"
    valid = tmp_path / "valid.sh"
    invalid = tmp_path / "invalid.sh"
    valid.write_text(f"printf 'executed' > '{marker.as_posix()}'\n", encoding="utf-8")
    invalid.write_text("if true; then\n", encoding="utf-8")
    assert checker.main([str(valid), str(invalid)]) != 0
    assert not marker.exists()


def test_missing_files_and_empty_arguments_fail_clearly(checker, tmp_path, capsys):
    assert checker.main([]) == 2
    assert "usage:" in capsys.readouterr().err
    assert checker.main([str(tmp_path / "missing.sh")]) == 1
    assert "shell-syntax:" in capsys.readouterr().err


def test_committed_hook_uses_the_platform_wrapper():
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    block = config.split("- id: shell-syntax", 1)[1].split("- id:", 1)[0]
    assert "entry: python scripts/hooks/check_shell.py" in block
    assert "language: system" in block


def test_checker_cli_accepts_repository_shell_scripts():
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/hooks/check_shell.py"),
            "scripts/deploy.sh",
            "scripts/check-host-nginx.sh",
            "scripts/verify-isolation.sh",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
