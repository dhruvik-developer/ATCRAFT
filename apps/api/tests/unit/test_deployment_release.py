"""Exercise guarded release orchestration without Docker or deployment secrets.

The notification worker writes durable delivery state. It must be drained with
the batch worker before migrations and be healthy before opening the edge.
Only function definitions are loaded; every container command is a shell stub.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

from src.core.config import REPO_ROOT

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def bash() -> str:
    spec = importlib.util.spec_from_file_location(
        "shell_syntax", REPO_ROOT / "scripts/hooks/check_shell.py"
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker.find_bash()


def _functions(*names: str) -> str:
    source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    definitions: list[str] = []
    for name in names:
        match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}", source, re.M | re.S)
        assert match, f"missing deployment function: {name}"
        definitions.append(match.group())
    return "\n".join(definitions)


def _run(
    bash: str,
    tmp_path: Path,
    command: str,
    *,
    running: str = "",
    fail_application_start: bool = False,
    existing_database: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    event_file = tmp_path / "commands.log"
    environment = {
        **os.environ,
        "DEPLOY_TEST_EVENTS": event_file.as_posix(),
        "DEPLOY_TEST_RUNNING": running,
        "DEPLOY_TEST_FAIL_START": str(int(fail_application_start)),
        "DEPLOY_TEST_EXISTING": str(int(existing_database)),
    }
    stubs = r"""
set -euo pipefail
PROJECT=atcraft-test
RELEASE_TAG=test-revision
DEPLOYMENT=main
LAST_BACKUP=
say() { :; }
info() { :; }
ok() { :; }
warn() { printf '%s\n' "$*" >&2; }
die() { printf '%s\n' "$*" >&2; exit 1; }
assert_clean_release_source() { :; }
assert_deployment_config() { :; }
start_existing_postgres() { [ "$DEPLOY_TEST_EXISTING" = 1 ]; }
create_backup() { printf '%s\n' backup >> "$DEPLOY_TEST_EVENTS"; }
docker() {
  if [ "$1" = ps ]; then
    [[ " $* " == *" label=com.docker.compose.project=$PROJECT "* ]] || return 9
    printf '%s' "$DEPLOY_TEST_RUNNING"
  fi
}
dc() {
  printf '%s\n' "$*" >> "$DEPLOY_TEST_EVENTS"
  if [ "$DEPLOY_TEST_FAIL_START" = 1 ] && [ "$1" = up ] &&
      [[ " $* " == *' celery-notify '* ]]; then return 1; fi
}
"""
    definitions = _functions(
        "running_application_services",
        "assert_application_stopped",
        "stop_application_traffic",
        "start_application_traffic",
        "release_up",
    )
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-s"],
        input=f"{stubs}\n{definitions}\n{command}\n",
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
        check=False,
    )
    events = event_file.read_text(encoding="utf-8").splitlines() if event_file.exists() else []
    return result, events


def test_running_writer_detection_includes_notifications_only_in_this_project(
    bash: str, tmp_path: Path
) -> None:
    result, _ = _run(
        bash,
        tmp_path,
        "running_application_services",
        running="postgres\nredis\ncelery-notify\ncelery-notify-extra\nbackend\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["celery-notify", "backend"]


def test_stop_drains_notification_worker_with_the_other_writers(bash: str, tmp_path: Path) -> None:
    result, events = _run(bash, tmp_path, "stop_application_traffic")
    assert result.returncode == 0, result.stderr
    assert events == ["stop -t 30 nginx frontend backend celery-worker celery-notify celery-beat"]


def test_a_notification_worker_still_running_blocks_schema_changes(
    bash: str, tmp_path: Path
) -> None:
    result, _ = _run(bash, tmp_path, "stop_application_traffic", running="celery-notify\n")
    assert result.returncode != 0
    assert "still running" in result.stderr
    assert "celery-notify" in result.stderr


def test_notification_readiness_precedes_opening_the_edge(bash: str, tmp_path: Path) -> None:
    result, events = _run(bash, tmp_path, "start_application_traffic")
    assert result.returncode == 0, result.stderr
    assert events == [
        "up -d --no-build --wait backend frontend celery-worker celery-notify celery-beat",
        "up -d --no-build --wait nginx",
    ]


def test_notification_start_failure_keeps_the_edge_stopped(bash: str, tmp_path: Path) -> None:
    result, events = _run(bash, tmp_path, "start_application_traffic", fail_application_start=True)
    assert result.returncode != 0
    assert events[-1] == "stop nginx"
    assert "up -d --no-build --wait nginx" not in events
    assert "Nginx remains stopped" in result.stderr


@pytest.mark.parametrize("existing_database", [False, True], ids=["fresh", "upgrade"])
def test_release_quiesces_notifications_before_migration_and_starts_them_before_traffic(
    bash: str, tmp_path: Path, existing_database: bool
) -> None:
    result, events = _run(bash, tmp_path, "release_up", existing_database=existing_database)
    assert result.returncode == 0, result.stderr
    stop = events.index("stop -t 30 nginx frontend backend celery-worker celery-notify celery-beat")
    migrate = events.index("run --rm --no-deps -T backend alembic upgrade head")
    start = events.index(
        "up -d --no-build --wait backend frontend celery-worker celery-notify celery-beat"
    )
    edge = events.index("up -d --no-build --wait nginx")
    assert stop < migrate < start < edge
    if existing_database:
        assert stop < events.index("backup") < migrate
    else:
        assert "backup" not in events
