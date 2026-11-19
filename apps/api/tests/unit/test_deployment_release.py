"""Exercise guarded release orchestration without Docker or deployment secrets.

The notification worker writes durable delivery state. It must be drained with
the batch worker before migrations and be healthy before opening the edge.

The one hosted deployment is production. The configuration gate accepted
``APP_ENV=staging`` - the removed dev deployment's value - for it, and
``demo-data`` asked before loading invented records only when the env file said
``production``, so an env file that said ``staging`` sent demo data into the
client's only database without a question. The gate now names the line to set,
and the question is asked whatever the file says.

Only function definitions and single command branches are loaded; every
container command is a shell stub, and the env file is a temporary one.
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


# -----------------------------------------------------------------------------
# Production is the only hosted deployment
# -----------------------------------------------------------------------------

#: Passes every other check in assert_deployment_config, so APP_ENV alone decides.
_DEPLOYMENT_ENV = """\
APP_ENV={app_env}
CORS_ORIGINS=https://erp.acme.test
ALLOWED_HOSTS=erp.acme.test
EMAIL_PROVIDER=smtp
PUBLIC_WEB_URL=https://erp.acme.test
EMAIL_FROM_ADDRESS=no-reply@acme.test
SMTP_HOST=smtp.acme.test
SMTP_SECURITY=starttls
SMTP_USERNAME=
SMTP_PASSWORD=
WHATSAPP_ENABLED=false
SMS_ENABLED=false
"""

_NOT_PRODUCTION = ["staging", "development", ""]


def _command(name: str) -> str:
    """One ``case "$COMMAND"`` branch of deploy.sh, as the function ``run_command``."""
    source = (REPO_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}\)\n(.*?)^    ;;$", source, re.M | re.S)
    assert match, f"missing deployment command: {name}"
    return f"run_command() {{\n{match.group(1)}}}"


def _shell(bash: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-s"],
        input=script,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def _check_config(bash: str, tmp_path: Path, app_env: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "main.env"
    env_file.write_text(_DEPLOYMENT_ENV.format(app_env=app_env), encoding="utf-8", newline="\n")
    stubs = r"""
set -euo pipefail
DEPLOYMENT=main
die() { printf '%s\n' "$*" >&2; exit 1; }
stat() { printf '600\n'; }
"""
    definitions = _functions("env_value", "assert_deployment_config")
    return _shell(
        bash,
        f"{stubs}ENV_FILE='{env_file.as_posix()}'\n{definitions}\nassert_deployment_config\n",
    )


def _demo_data(
    bash: str, tmp_path: Path, app_env: str, answer: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    events = tmp_path / "commands.log"
    answer_file = tmp_path / "answer.txt"
    answer_file.write_text(answer, encoding="utf-8", newline="\n")
    stubs = r"""
set -euo pipefail
DEPLOYMENT=main
C_YELLOW=
C_RESET=
warn() { printf '%s\n' "$*" >&2; }
die() { printf '%s\n' "$*" >&2; exit 1; }
dc() { printf '%s\n' "$*" >> "$DEPLOY_TEST_EVENTS"; }
env_value() { if [ "$1" = APP_ENV ]; then printf '%s' "$DEPLOY_TEST_APP_ENV"; fi; }
"""
    result = _shell(
        bash,
        f"{stubs}DEPLOY_TEST_EVENTS='{events.as_posix()}'\n"
        f"DEPLOY_TEST_APP_ENV='{app_env}'\n"
        f"{_functions('confirm')}\n{_command('demo-data')}\n"
        f"run_command --companies 1 < '{answer_file.as_posix()}'\n",
    )
    ran = events.read_text(encoding="utf-8").splitlines() if events.exists() else []
    return result, ran


@pytest.mark.parametrize("app_env", _NOT_PRODUCTION, ids=lambda value: value or "empty")
def test_an_env_file_that_is_not_production_is_refused_with_the_line_to_set(
    bash: str, tmp_path: Path, app_env: str
) -> None:
    result = _check_config(bash, tmp_path, app_env)
    assert result.returncode != 0
    assert f"APP_ENV is '{app_env or 'empty'}'" in result.stderr
    assert "Set the line to: APP_ENV=production" in result.stderr


def test_a_production_env_file_passes_the_configuration_gate(bash: str, tmp_path: Path) -> None:
    result = _check_config(bash, tmp_path, "production")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "app_env", ["production", *_NOT_PRODUCTION], ids=lambda value: value or "empty"
)
def test_demo_data_asks_before_loading_whatever_app_env_says(
    bash: str, tmp_path: Path, app_env: str
) -> None:
    result, ran = _demo_data(bash, tmp_path, app_env, answer="")
    assert result.returncode != 0
    assert "Load invented business data" in result.stderr
    assert "cancelled" in result.stderr
    assert ran == []


def test_demo_data_loads_once_the_question_is_answered(bash: str, tmp_path: Path) -> None:
    result, ran = _demo_data(bash, tmp_path, "staging", answer="yes load demo data into main\n")
    assert result.returncode == 0, result.stderr
    assert ran == ["run --rm --no-deps -T backend python -m src.core.demo_data --companies 1"]
