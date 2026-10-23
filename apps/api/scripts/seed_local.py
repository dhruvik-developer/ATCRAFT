"""Add the complete demonstration business to a local database, without resetting it.

Run from ``apps/api`` with ``python -m scripts.seed_local``. Completed demo
documents survive a failed later stage; deterministic fixture identifiers make
rerunning this command the recovery path. Existing passwords are preserved.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import ipaddress
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

try:
    from src.core.config import AppEnv, EmailProvider, Settings, settings
except ValueError:
    if __name__ == "__main__":
        # Pydantic's default traceback can include raw environment inputs.
        print(
            "Local seed refused: application settings are invalid. "
            "Check the local .env configuration before rerunning.",
            file=sys.stderr,
        )
        sys.exit(2)
    raise
from src.core.errors import ValidationError
from src.core.logging import configure_logging

API_ROOT = Path(__file__).resolve().parents[1]
_ROUTING_PARAMETERS = frozenset(
    {"host", "hostaddr", "port", "dbname", "database", "service", "servicefile", "dsn", "conninfo"}
)
_ISOLATED_SETTINGS: dict[str, Any] = {
    "email_provider": EmailProvider.disabled,
    "web_push_enabled": False,
    "cache_enabled": False,
    "rate_limit_enabled": False,
    "celery_broker_url": "memory://",
    "celery_result_backend": "cache+memory://",
    "otel_enabled": False,
    "sentry_dsn": "",
    "db_echo": False,
}


@dataclass(frozen=True, slots=True)
class LocalTarget:
    host: str
    port: int
    database: str


@dataclass(frozen=True, slots=True)
class SeedOptions:
    company: str
    company_code: str
    admin_email: str
    admin_password: str | None = field(repr=False)


def _local_url(value: str, *, driver: str, setting: str) -> LocalTarget:
    try:
        url: URL = make_url(value)
        port = url.port if url.port is not None else 5432
    except (ArgumentError, ValueError) as exc:
        raise ValueError(f"{setting} must be a valid PostgreSQL URL.") from exc
    if url.drivername != driver:
        raise ValueError(f"{setting} must use the {driver} driver.")
    if not 1 <= port <= 65535:
        raise ValueError(f"{setting} must use a port between 1 and 65535.")
    if not url.host or not url.database or not url.database.strip():
        raise ValueError(f"{setting} must explicitly name its host and database.")
    if any(key.lower() in _ROUTING_PARAMETERS for key in url.query):
        raise ValueError(f"{setting} cannot contain connection-routing query parameters.")
    host = url.host.lower().removesuffix(".")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(
                f"{setting} must point to localhost or a loopback IP address."
            ) from exc
        if not address.is_loopback or "%" in host:
            raise ValueError(f"{setting} must point to localhost or a loopback IP address.")
        host = str(address)
    return LocalTarget(host, port, url.database)


def validate_local_target(config: Settings) -> LocalTarget:
    """Refuse deployed or ambiguous targets before migrations or fixture writes.

    Parse both URLs instead of searching their text: credentials and misleading
    DNS suffixes can contain ``localhost`` too. Libpq routing overrides would
    undermine the explicit host, so the local CLI refuses those as well.
    """
    if config.app_env not in {AppEnv.development, AppEnv.test}:
        raise ValueError("Local seeding is allowed only with APP_ENV=development or test.")
    if any(os.environ.get(key) for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")):
        raise ValueError("Unset PGHOSTADDR, PGSERVICE and PGSERVICEFILE before local seeding.")
    asynchronous = _local_url(
        config.database_url, driver="postgresql+asyncpg", setting="DATABASE_URL"
    )
    synchronous = _local_url(
        config.database_sync_url, driver="postgresql+psycopg", setting="DATABASE_SYNC_URL"
    )
    if asynchronous != synchronous:
        raise ValueError(
            "DATABASE_URL and DATABASE_SYNC_URL must name the same host, port and database."
        )
    return asynchronous


@contextmanager
def isolated_seed_process() -> Iterator[None]:
    """Keep fixture notifications and queued work away from external transports.

    Environment overrides also apply to the PDF child process. Restore settings
    for callers embedding the CLI in tests; separate running API/worker processes
    never observe these process-local changes.
    """
    previous = {key: getattr(settings, key) for key in _ISOLATED_SETTINGS}
    environment = {key.upper(): os.environ.get(key.upper()) for key in _ISOLATED_SETTINGS}
    celery_module = sys.modules.get("src.workers.celery_app")
    celery_config = celery_module.celery_app.conf if celery_module else None
    celery_previous = (
        (celery_config.broker_url, celery_config.result_backend) if celery_config else None
    )
    database_module = sys.modules.get("src.core.database")
    previous_echo = database_module.engine.echo if database_module else None
    try:
        for key, value in _ISOLATED_SETTINGS.items():
            setattr(settings, key, value)
            os.environ[key.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
        if celery_config is not None:
            celery_config.update(broker_url="memory://", result_backend="cache+memory://")
        if database_module is not None:
            database_module.engine.echo = False
        yield
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)
        for key, value in environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if celery_config is not None and celery_previous is not None:
            celery_config.update(broker_url=celery_previous[0], result_backend=celery_previous[1])
        if database_module is not None:
            database_module.engine.echo = previous_echo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--company", default="Atcraft", help="Company display name")
    parser.add_argument("--company-code", "--code", default="ATCRAFT", help="Company code")
    parser.add_argument("--admin-email", default=None, help="Administrator email")
    parser.add_argument(
        "--admin-password",
        default=None,
        help=(
            "Initial password. Omit to generate and show one once; "
            "existing passwords stay unchanged."
        ),
    )
    return parser


def _options(arguments: argparse.Namespace) -> SeedOptions:
    from src.common.codes import clean_name, validate_code
    from src.core.security import validate_password_policy
    from src.core.seed import DEFAULT_ADMIN_EMAIL

    company = clean_name(arguments.company)
    code = validate_code(arguments.company_code)
    if len(company) > 255 or len(code) > 32:
        raise ValueError("Company name must be at most 255 characters and code at most 32.")
    try:
        email = str(
            TypeAdapter(EmailStr).validate_python(arguments.admin_email or DEFAULT_ADMIN_EMAIL)
        ).lower()
    except PydanticValidationError as exc:
        raise ValueError("--admin-email must be a valid email address.") from exc
    if arguments.admin_password is not None:
        validate_password_policy(arguments.admin_password, email=email, field="admin_password")
    return SeedOptions(company, code, email, arguments.admin_password)


def _upgrade_schema() -> None:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    command.upgrade(config, "head")


async def _seed_application(options: SeedOptions) -> None:
    from src.core import demo_data, seed
    from src.core.cache import close_redis
    from src.core.database import dispose_engine
    from src.core.demo_data.summaries import refresh_if_stale

    try:
        print("Local seed: baseline reference data")
        # The baseline CLI prints its initial password. Preserve the intentional
        # generated-password handover, but never echo a caller-supplied secret.
        if options.admin_password is not None:
            with redirect_stdout(io.StringIO()):
                await seed.seed(
                    options.company,
                    options.company_code,
                    options.admin_email,
                    options.admin_password,
                )
            print("Baseline ready; supplied password was not displayed.")
        else:
            await seed.seed(options.company, options.company_code, options.admin_email, None)
        print("Local seed: demonstration workflows")
        context = await demo_data.run(options.admin_email, company_code=options.company_code)
        await refresh_if_stale(context.tenant)
        demo_data._report(context)
    finally:
        try:
            await close_redis()
        finally:
            await dispose_engine()


def _render_reports(options: SeedOptions) -> None:
    result = subprocess.run(  # noqa: S603 - fixed module; validated email is one argv element
        [
            sys.executable,
            "-m",
            "src.core.demo_data.reports",
            "--admin-email",
            options.admin_email,
            "--company-code",
            options.company_code,
        ],
        cwd=API_ROOT,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError("Seeded PDF rendering did not complete.")
    if result.stdout:
        print(result.stdout, end="")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    phase = "preflight"
    try:
        target = validate_local_target(settings)
        with isolated_seed_process():
            options = _options(arguments)
            configure_logging()
            print(f"Local seed: {target.database} on {target.host}:{target.port}")
            phase = "schema migrations"
            print(f"Local seed: {phase}")
            _upgrade_schema()
            phase = "baseline and demonstration workflows"
            # Both async flows share one loop and dispose their pooled resources
            # before report tasks start in their own isolated Python process.
            asyncio.run(_seed_application(options))
            phase = "seeded PDF reports"
            print(f"Local seed: {phase}")
            _render_reports(options)
    except (ValueError, ValidationError) as exc:
        if phase == "preflight":
            message = exc.message if isinstance(exc, ValidationError) else str(exc)
            print(f"Local seed refused: {message}", file=sys.stderr)
            return 2
        print(
            f"Local seed failed during {phase} ({type(exc).__name__}). "
            "Review the last completed stage, correct its inputs, then rerun the same command. "
            "Completed data is preserved.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        # Driver errors can contain SQL parameters, credentials or connection
        # URLs. The CLI reports the phase/type without exposing that payload.
        print(
            f"Local seed failed during {phase} ({type(exc).__name__}). "
            "Check local database availability, migration status and the last completed stage, "
            "then rerun the same command. Completed data is preserved.",
            file=sys.stderr,
        )
        return 1
    print("Local seed complete. Review any skipped workflow messages above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
