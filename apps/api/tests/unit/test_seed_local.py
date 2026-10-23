"""Local seeding cannot redirect writes or expose an explicitly supplied secret."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from scripts import seed_local

from src.core.config import AppEnv, EmailProvider, settings

pytestmark = pytest.mark.unit

PASSWORD = "Cedar!8427Example"


@pytest.fixture(autouse=True)
def local_configuration(monkeypatch):
    monkeypatch.setattr(seed_local, "configure_logging", Mock())
    monkeypatch.setattr(settings, "app_env", AppEnv.test)
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://user:secret@127.0.0.1:5433/local_seed"
    )
    monkeypatch.setattr(
        settings, "database_sync_url", "postgresql+psycopg://user:secret@127.0.0.1:5433/local_seed"
    )
    for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("environment", [AppEnv.staging, AppEnv.production])
def test_deployed_environments_are_refused(environment, monkeypatch):
    monkeypatch.setattr(settings, "app_env", environment)
    with pytest.raises(ValueError, match="APP_ENV"):
        seed_local.validate_local_target(settings)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "LOCALHOST.", "[::1]"])
def test_explicit_matching_loopback_targets_are_accepted(host, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"postgresql+asyncpg://user@{host}/seed")
    monkeypatch.setattr(settings, "database_sync_url", f"postgresql+psycopg://user@{host}/seed")
    target = seed_local.validate_local_target(settings)
    assert target.database == "seed"
    assert target.port == 5432


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://localhost:secret@database.example/seed",
        "postgresql+asyncpg://localhost.attacker.example/seed",
        "postgresql+asyncpg://192.168.1.20/seed",
        "postgresql+asyncpg://[::]/seed",
        "postgresql+asyncpg://[::1%25eth0]/seed",
        "postgresql+asyncpg:///seed",
        "postgresql+asyncpg://localhost",
        "postgresql+asyncpg://localhost/",
        "postgresql+asyncpg://localhost:bad/seed",
        "postgresql+asyncpg://localhost:0/seed",
        "postgresql+asyncpg://localhost:65536/seed",
        "not a database URL",
        "sqlite:///seed",
        "postgresql://localhost/seed",
    ],
)
def test_ambiguous_remote_or_invalid_urls_are_refused_without_echoing_them(url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", url)
    with pytest.raises(ValueError) as caught:
        seed_local.validate_local_target(settings)
    assert "secret" not in str(caught.value)
    assert url not in str(caught.value)


@pytest.mark.parametrize("parameter", sorted(seed_local._ROUTING_PARAMETERS))
@pytest.mark.parametrize("attribute", ["database_url", "database_sync_url"])
def test_connection_routing_query_parameters_cannot_override_local_target(
    parameter, attribute, monkeypatch
):
    monkeypatch.setattr(settings, attribute, getattr(settings, attribute) + f"?{parameter}=remote")
    with pytest.raises(ValueError, match="routing"):
        seed_local.validate_local_target(settings)


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_libpq_environment_routing_overrides_are_refused(name, monkeypatch):
    monkeypatch.setenv(name, "remote")
    with pytest.raises(ValueError, match="Unset PG"):
        seed_local.validate_local_target(settings)


@pytest.mark.parametrize(
    "replacement",
    [
        "postgresql+psycopg://user@localhost:5433/local_seed",
        "postgresql+psycopg://user@127.0.0.1:5432/local_seed",
        "postgresql+psycopg://user@127.0.0.1:5433/other_seed",
    ],
)
def test_both_drivers_must_target_the_same_explicit_database(replacement, monkeypatch):
    monkeypatch.setattr(settings, "database_sync_url", replacement)
    with pytest.raises(ValueError, match="same host, port and database"):
        seed_local.validate_local_target(settings)


def test_process_transport_overrides_restore_settings_environment_and_existing_clients(monkeypatch):
    original = {key: getattr(settings, key) for key in seed_local._ISOLATED_SETTINGS}
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)

    class Configuration(SimpleNamespace):
        def update(self, **values):
            self.__dict__.update(values)

    configuration = Configuration(broker_url="redis://broker/1", result_backend="redis://broker/2")
    monkeypatch.setitem(
        sys.modules,
        "src.workers.celery_app",
        SimpleNamespace(celery_app=SimpleNamespace(conf=configuration)),
    )
    engine = SimpleNamespace(echo=True)
    monkeypatch.setitem(sys.modules, "src.core.database", SimpleNamespace(engine=engine))
    with pytest.raises(RuntimeError), seed_local.isolated_seed_process():
        assert settings.email_provider == EmailProvider.disabled
        assert settings.web_push_enabled is False
        assert settings.cache_enabled is False
        assert settings.rate_limit_enabled is False
        assert settings.db_echo is False
        assert settings.celery_broker_url == configuration.broker_url == "memory://"
        assert settings.celery_result_backend == configuration.result_backend == "cache+memory://"
        assert os.environ["EMAIL_PROVIDER"] == "disabled"
        assert os.environ["DB_ECHO"] == "false"
        assert engine.echo is False
        raise RuntimeError("fixture failure")
    assert {key: getattr(settings, key) for key in original} == original
    assert os.environ["EMAIL_PROVIDER"] == "smtp"
    assert "CELERY_BROKER_URL" not in os.environ
    assert configuration.broker_url == "redis://broker/1"
    assert configuration.result_backend == "redis://broker/2"
    assert engine.echo is True


def test_arguments_reuse_shared_normalization_and_password_policy():
    options = seed_local._options(
        argparse.Namespace(
            company="  Example   Company ",
            company_code=" example ",
            admin_email=" Admin@Example.com ",
            admin_password=PASSWORD,
        )
    )
    assert options.company == "Example Company"
    assert options.company_code == "EXAMPLE"
    assert options.admin_email == "admin@example.com"
    assert PASSWORD not in repr(options)
    with pytest.raises(seed_local.ValidationError, match="Password"):
        seed_local._options(
            argparse.Namespace(
                company="Example", company_code="EXAMPLE", admin_email=None, admin_password="weak"
            )
        )


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (["--company", " "], "blank"),
        (["--company", "a" * 256], "at most"),
        (["--code", "a" * 33], "at most"),
        (["--code", "!"], "Code"),
        (["--admin-email", "not an email"], "valid email"),
        (["--admin-password", "weak"], "Password"),
    ],
)
def test_invalid_input_is_refused_before_migrations(arguments, expected, monkeypatch, capsys):
    migrate = Mock()
    monkeypatch.setattr(seed_local, "_upgrade_schema", migrate)
    assert seed_local.main(arguments) == 2
    migrate.assert_not_called()
    captured = capsys.readouterr()
    assert expected in captured.err
    assert "weak" not in captured.err


def test_unsafe_target_cannot_start_any_stage(monkeypatch, capsys):
    monkeypatch.setattr(settings, "app_env", AppEnv.production)
    migrate = Mock()
    monkeypatch.setattr(seed_local, "_upgrade_schema", migrate)
    assert seed_local.main([]) == 2
    migrate.assert_not_called()
    assert "APP_ENV" in capsys.readouterr().err


def test_pipeline_runs_migrations_then_async_seeds_then_reports_in_isolation(monkeypatch, capsys):
    calls = []

    def migrate():
        calls.append("migrate")
        assert settings.celery_broker_url == "memory://"

    async def seed(options):
        calls.append(("seed", options.admin_email))
        assert settings.web_push_enabled is False

    def reports(options):
        calls.append(("reports", options.admin_email))
        assert os.environ["CELERY_BROKER_URL"] == "memory://"

    monkeypatch.setattr(seed_local, "_upgrade_schema", migrate)
    monkeypatch.setattr(seed_local, "_seed_application", seed)
    monkeypatch.setattr(seed_local, "_render_reports", reports)
    assert (
        seed_local.main(["--admin-email", "Admin@Example.com", "--admin-password", PASSWORD]) == 0
    )
    assert calls == ["migrate", ("seed", "admin@example.com"), ("reports", "admin@example.com")]
    assert PASSWORD not in str(capsys.readouterr())


@pytest.mark.parametrize("password", [None, PASSWORD])
async def test_async_stages_share_one_loop_dispose_resources_and_only_show_generated_password(
    password, monkeypatch, capsys
):
    from src.core import cache, database, demo_data, seed
    from src.core.demo_data import summaries

    loops = []
    tenant = SimpleNamespace()

    async def baseline(*arguments):
        loops.append(asyncio.get_running_loop())
        sys.stdout.write((arguments[-1] or "generated-once") + "\n")

    async def demo(email, *, company_code):
        loops.append(asyncio.get_running_loop())
        assert email == "admin@example.com"
        assert company_code == "EXAMPLE"
        return SimpleNamespace(tenant=tenant)

    close = AsyncMock()
    dispose = AsyncMock()
    report = Mock()
    refresh = AsyncMock()
    monkeypatch.setattr(seed, "seed", baseline)
    monkeypatch.setattr(demo_data, "run", demo)
    monkeypatch.setattr(demo_data, "_report", report)
    monkeypatch.setattr(cache, "close_redis", close)
    monkeypatch.setattr(database, "dispose_engine", dispose)
    monkeypatch.setattr(summaries, "refresh_if_stale", refresh)
    await seed_local._seed_application(
        seed_local.SeedOptions("Example", "EXAMPLE", "admin@example.com", password)
    )
    assert loops[0] is loops[1]
    close.assert_awaited_once()
    dispose.assert_awaited_once()
    refresh.assert_awaited_once_with(tenant)
    report.assert_called_once()
    output = capsys.readouterr().out
    assert PASSWORD not in output
    assert output.count("generated-once") == (1 if password is None else 0)


async def test_async_failure_still_disposes_resources(monkeypatch):
    from src.core import cache, database, seed

    monkeypatch.setattr(seed, "seed", AsyncMock(side_effect=RuntimeError("failure")))
    close = AsyncMock(side_effect=RuntimeError("cache cleanup failure"))
    dispose = AsyncMock()
    monkeypatch.setattr(cache, "close_redis", close)
    monkeypatch.setattr(database, "dispose_engine", dispose)
    with pytest.raises(RuntimeError):
        await seed_local._seed_application(
            seed_local.SeedOptions("Example", "EXAMPLE", "admin@example.com", PASSWORD)
        )
    dispose.assert_awaited_once()


def test_report_child_inherits_only_process_isolated_transports(monkeypatch, capsys):
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout="Rendered 4 PDFs\n", stderr="")
    )
    monkeypatch.setattr(seed_local.subprocess, "run", run)
    with seed_local.isolated_seed_process():
        seed_local._render_reports(
            seed_local.SeedOptions("Example", "EXAMPLE", "admin@example.com", PASSWORD)
        )
    arguments, keywords = run.call_args
    assert arguments[0][-4:] == ["--admin-email", "admin@example.com", "--company-code", "EXAMPLE"]
    assert PASSWORD not in repr(arguments)
    assert keywords["env"]["CELERY_BROKER_URL"] == "memory://"
    assert keywords["env"]["EMAIL_PROVIDER"] == "disabled"
    assert keywords["env"]["WEB_PUSH_ENABLED"] == "false"
    assert capsys.readouterr().out == "Rendered 4 PDFs\n"


@pytest.mark.parametrize("exception", [RuntimeError(PASSWORD), ValueError(PASSWORD)])
def test_failure_is_actionable_without_exception_payload_or_secret(exception, monkeypatch, capsys):
    monkeypatch.setattr(seed_local, "_upgrade_schema", Mock(side_effect=exception))
    assert seed_local.main([]) == 1
    captured = capsys.readouterr()
    assert "schema migrations" in captured.err
    assert "rerun the same command" in captured.err
    assert "Completed data is preserved" in captured.err
    assert PASSWORD not in str(captured)


def test_report_failure_does_not_echo_child_diagnostics(monkeypatch, capsys):
    monkeypatch.setattr(
        seed_local.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, stdout=PASSWORD, stderr=PASSWORD)),
    )
    with pytest.raises(RuntimeError, match="rendering"):
        seed_local._render_reports(
            seed_local.SeedOptions("Example", "EXAMPLE", "admin@example.com", PASSWORD)
        )
    assert PASSWORD not in str(capsys.readouterr())


def test_cli_has_no_reset_or_guard_override():
    help_text = seed_local._parser().format_help()
    for option in ("--reset", "--drop", "--force", "--i-know-what-i-am-doing", "--yes"):
        assert option not in help_text


def test_migration_phase_uses_existing_alembic_chain_from_any_working_directory(monkeypatch):
    upgrade = Mock()
    monkeypatch.setattr(seed_local.command, "upgrade", upgrade)
    seed_local._upgrade_schema()
    configuration, revision = upgrade.call_args.args
    assert revision == "head"
    assert configuration.config_file_name == str(seed_local.API_ROOT / "alembic.ini")
    assert configuration.get_main_option("script_location") == str(
        seed_local.API_ROOT / "migrations"
    )


def test_invalid_settings_bootstrap_does_not_echo_environment_secrets():
    environment = dict(os.environ)
    secret = "configuration-secret-must-not-be-displayed"
    environment["LOG_LEVEL"] = secret
    result = subprocess.run(
        [sys.executable, "-m", "scripts.seed_local"],
        cwd=seed_local.API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "application settings are invalid" in result.stderr
    assert secret not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
