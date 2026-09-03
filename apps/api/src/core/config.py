"""Typed application configuration.

Master rules section 47/69: every secret and environment-specific value arrives
through the environment. Nothing is hardcoded, and no module calls ``os.getenv``
directly - they import :data:`settings` instead, so the full configuration
surface is visible and validated in one place.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _ancestor(module_path: Path, levels: int) -> Path:
    """The directory ``levels`` above this file, clamped to the filesystem root.

    Both roots below are found by counting directories up from this file, which
    is correct in a checkout and wrong in the container image: ``src/`` is
    copied straight to ``/app``, so there are fewer parents to count than the
    checkout layout has. ``parents[4]`` then raises ``IndexError`` while this
    module is still being imported - before any logger or exception handler
    exists - so the application could not boot at all. Gunicorn reported
    "Worker failed to boot" above a pathlib traceback that mentioned neither
    configuration nor a missing file.

    It survived every test, a clean lint and a green build because all of them
    run from a checkout. The layout is the input, and only one layout had ever
    been supplied; the first thing to import this module from an installed
    layout was the deployed image itself.

    Clamping rather than special-casing the two known layouts: this is a
    "how far up can I go" question, the answer is never an error, and a
    predicate would just be one more thing to get wrong for the next layout.
    Both roots are used only to locate an optional ``.env``, and a deployed
    environment passes real environment variables and needs no file - so a
    clamped path pointing at a ``.env`` that does not exist is an ordinary
    case, which pydantic-settings ignores.
    """
    parents = module_path.parents
    return parents[min(levels, len(parents) - 1)]


def _api_root(module_path: Path) -> Path:
    """The directory holding ``src/``: ``apps/api`` in a checkout, ``/app`` in
    the image."""
    return _ancestor(module_path, 2)


def _repo_root(module_path: Path) -> Path:
    """The repository root in a checkout; the nearest thing to it otherwise."""
    return _ancestor(module_path, 4)


_MODULE_PATH = Path(__file__).resolve()
API_ROOT = _api_root(_MODULE_PATH)
REPO_ROOT = _repo_root(_MODULE_PATH)


class AppEnv(StrEnum):
    development = "development"
    staging = "staging"
    production = "production"
    test = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Local development reads a single `.env` at the repository root - the
        # same file Docker Compose picks up, so the app and the containers
        # cannot disagree about which database they mean.
        #
        # Staging and production do NOT use files: the compose overlays inject
        # real environment variables, which always win over anything here.
        #
        # Order matters and is easy to get backwards: in pydantic-settings the
        # LAST file wins, so the api-local override is listed last.
        env_file=(
            REPO_ROOT / ".env",
            API_ROOT / ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app --
    app_env: AppEnv = AppEnv.development
    app_name: str = "Atcraft Manufacturing ERP"
    app_debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False

    # ----------------------------------------------------------- database --
    database_url: str = "postgresql+asyncpg://atcraft:atcraft@localhost:5432/atcraft_erp"
    database_sync_url: str = "postgresql+psycopg://atcraft:atcraft@localhost:5432/atcraft_erp"
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 10
    db_pool_timeout: Annotated[int, Field(ge=1)] = 30
    db_pool_recycle: Annotated[int, Field(ge=60)] = 1800
    db_echo: bool = False
    db_statement_timeout_ms: Annotated[int, Field(ge=1000)] = 30_000

    # -------------------------------------------------------------- redis --
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    cache_enabled: bool = True
    cache_default_ttl_seconds: Annotated[int, Field(ge=1)] = 300

    # ----------------------------------------------------------- security --
    jwt_secret: str = Field(default="", repr=False)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_access_token_expire_minutes: Annotated[int, Field(ge=1, le=1440)] = 15
    jwt_refresh_token_expire_days: Annotated[int, Field(ge=1, le=90)] = 7
    jwt_issuer: str = "atcraft-erp"
    jwt_audience: str = "atcraft-erp-api"
    password_min_length: Annotated[int, Field(ge=8)] = 12
    login_max_failed_attempts: Annotated[int, Field(ge=1)] = 5
    login_lockout_minutes: Annotated[int, Field(ge=1)] = 15
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: Annotated[int, Field(ge=1)] = 120
    rate_limit_auth_per_minute: Annotated[int, Field(ge=1)] = 10

    # --------------------------------------------------------------- cors --
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    cors_allow_credentials: bool = True

    # ------------------------------------------------------------ storage --
    storage_path: Path = REPO_ROOT / "storage"
    max_upload_size_mb: Annotated[int, Field(ge=1, le=500)] = 25
    allowed_upload_mime_types: str = (
        "image/jpeg,image/png,image/webp,application/pdf,text/csv,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    # ----------------------------------------------------------- business --
    default_currency: str = "INR"
    money_decimal_places: Annotated[int, Field(ge=0, le=6)] = 2
    quantity_decimal_places: Annotated[int, Field(ge=0, le=6)] = 4
    # Above this many demand lines an MRP run is dispatched to Celery rather
    # than executed inline (section 14: never hold HTTP open for minutes).
    mrp_sync_threshold_demand_lines: Annotated[int, Field(ge=1)] = 50

    # ------------------------------------------------------ observability --
    otel_enabled: bool = False
    otel_service_name: str = "atcraft-erp-api"
    otel_exporter_otlp_endpoint: str = ""
    prometheus_metrics_enabled: bool = True
    sentry_dsn: str = ""

    # ------------------------------------------------------------ derived --
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.production

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_mime_type_set(self) -> frozenset[str]:
        return frozenset(
            m.strip().lower() for m in self.allowed_upload_mime_types.split(",") if m.strip()
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_upload_size_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    # --------------------------------------------------------- validation --
    @field_validator("database_url", "database_sync_url")
    @classmethod
    def _validate_pg_url(cls, v: str, info: object) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("PostgreSQL is the only supported database (master rules section 3)")
        return v

    @field_validator("storage_path")
    @classmethod
    def _resolve_storage(cls, v: Path) -> Path:
        return v.expanduser().resolve()

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        """Refuse to boot a production process with development-grade secrets.

        A misconfigured production deployment is a security incident, not a
        warning - so this fails loudly at import time rather than serving
        traffic with a guessable signing key.
        """
        weak = {"", "change-me", "changeme", "secret", "CHANGE-ME-generate-a-64-byte-random-secret"}
        if self.app_env is AppEnv.production:
            if self.jwt_secret in weak or len(self.jwt_secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be a strong random value (>=32 chars) in production. "
                    "Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(64))"'
                )
            if self.app_debug:
                raise ValueError("APP_DEBUG must be false in production")
            if "*" in self.cors_origin_list and self.cors_allow_credentials:
                raise ValueError("Wildcard CORS origin cannot be combined with credentials")
        elif not self.jwt_secret:
            # Development convenience only: an ephemeral per-process key. Tokens
            # do not survive a restart, which is the correct local behaviour.
            object.__setattr__(self, "jwt_secret", secrets.token_urlsafe(64))
        return self

    def ensure_storage_dirs(self) -> None:
        """Create the storage tree described in master rules section 43."""
        for sub in ("documents", "reports", "attachments", "exports", "temp"):
            (self.storage_path / sub).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor - configuration is read once per process."""
    return Settings()


settings = get_settings()

__all__ = ["API_ROOT", "REPO_ROOT", "AppEnv", "Settings", "get_settings", "settings"]
