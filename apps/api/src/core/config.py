"""Typed application configuration.

Master rules section 47/69: every secret and environment-specific value arrives
through the environment. Nothing is hardcoded, and no module calls ``os.getenv``
directly - they import :data:`settings` instead, so the full configuration
surface is visible and validated in one place.
"""

from __future__ import annotations

import re
import secrets
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BeforeValidator,
    EmailStr,
    Field,
    HttpUrl,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.constants import DEFAULT_CURRENCY


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


class EmailProvider(StrEnum):
    """Outbound mail implementation selected for this deployment."""

    disabled = "disabled"
    smtp = "smtp"


class SmtpSecurity(StrEnum):
    """How the SMTP connection is protected in transit."""

    none = "none"
    starttls = "starttls"
    tls = "tls"


def _blank_to_none(value: object) -> object:
    """Let optional typed settings be represented by an empty env value."""
    return None if value == "" else value


OptionalEmail = Annotated[EmailStr | None, BeforeValidator(_blank_to_none)]

#: Whole values that are never a secret, whatever their length.
_WEAK_SECRETS = frozenset({"", "change-me", "changeme", "secret", "password"})

#: Substrings that mark a value as a template placeholder rather than something
#: generated. Matched case-insensitively against the whole value, so a
#: re-worded placeholder is refused the day it is written into a template - the
#: previous exact-match list let ``CHANGE-ME-generate-a-different-64-byte-
#: random-secret`` through because it was 52 characters and not spelled the
#: same as the JWT one. A ``token_urlsafe`` value cannot contain a hyphenated
#: English phrase, so nothing generated is ever refused by this.
_PLACEHOLDER_MARKERS = ("change-me", "change_me", "changeme", "generate-a", "placeholder")

#: Shortest secret accepted by a deployed process, in characters.
MIN_DEPLOYED_SECRET_LENGTH = 32


def is_placeholder_secret(value: str) -> bool:
    """Would a deployed process be booting on a value copied from a template?"""
    if value in _WEAK_SECRETS or len(value) < MIN_DEPLOYED_SECRET_LENGTH:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


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
    # How often `maintenance.allocate_short_orders` reserves newly available
    # stock for short sales orders (docs/assumptions.md section E1-2). A minute
    # is the floor: the sweep reads every tenant's open order book.
    so_topup_interval_seconds: Annotated[int, Field(ge=60)] = 900
    cache_enabled: bool = True
    cache_default_ttl_seconds: Annotated[int, Field(ge=1)] = 300

    # ----------------------------------------------------------- security --
    jwt_secret: str = Field(default="", repr=False)
    # Encrypts long-lived credentials stored in the database (currently TOTP
    # seeds and the short-lived delivery copy of a password-reset token).  This
    # key is deliberately independent from JWT signing so routine signing-key
    # rotation cannot make every enrolled authenticator unreadable.
    credential_encryption_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_access_token_expire_minutes: Annotated[int, Field(ge=1, le=1440)] = 15
    jwt_refresh_token_expire_days: Annotated[int, Field(ge=1, le=90)] = 7
    jwt_issuer: str = "atcraft-erp"
    jwt_audience: str = "atcraft-erp-api"
    password_min_length: Annotated[int, Field(ge=8)] = 12
    login_max_failed_attempts: Annotated[int, Field(ge=1)] = 5
    login_lockout_minutes: Annotated[int, Field(ge=1)] = 15
    rate_limit_enabled: bool = True
    # Per-principal API budget, keyed on the authenticated user where there is
    # one (see ``RateLimitMiddleware``), so this is a budget for one person and
    # not for an office. Sized from what the screens actually cost: a detail
    # page fires about five queries and opening a dialog on it fires five more,
    # so a clerk moving briskly through fifteen documents a minute spends ~150.
    # The old 120 sat below that, which made a fast operator - the one doing the
    # job properly - the first person to see "Too many requests".
    rate_limit_default_per_minute: Annotated[int, Field(ge=1)] = 300
    # Per-address budget for unauthenticated sign-in attempts, expressed per
    # minute and applied by ``RateLimitPolicy.LOGIN_PER_IP`` over its
    # five-minute sliding window (20/min -> 100 per 5 min).
    #
    # This is the budget an entire plant shares, because everyone behind one NAT
    # arrives from one address, and a shift change is the worst case: fifty
    # people signing in inside five minutes, some twice. At the previous 4/min
    # (20 per window) the twenty-first person was refused, and refused *before*
    # their password was ever checked - indistinguishable, from the floor, from
    # the ERP being down. 100 per window covers that shift change with room for
    # retries.
    #
    # Raising it does not weaken the defence that matters. Credential stuffing
    # is stopped per account - ``LOGIN_PER_EMAIL`` (5 per 5 min) and the
    # lockout after ``login_max_failed_attempts`` - and both are fixed security
    # constants. What this budget bounds is *spraying* many accounts from one
    # address, which it still caps at 20 distinct attempts a minute.
    rate_limit_auth_per_minute: Annotated[int, Field(ge=1)] = 20
    # Comma-separated hostnames accepted by TrustedHostMiddleware.  Public
    # hostnames are normally listed explicitly by the deployment; CORS origins
    # are also folded in below so existing deployments fail closed rather than
    # becoming unreachable when upgrading.
    allowed_hosts: str = ""

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

    # --------------------------------------------------------------- email --
    # Mail is intentionally disabled for an unconfigured developer checkout.
    # Production validation below refuses that state, because the public
    # password-reset endpoint must never claim that a link was sent when no
    # delivery transport exists.
    email_provider: EmailProvider = EmailProvider.disabled
    public_web_url: HttpUrl = HttpUrl("http://localhost:5173")
    email_from_address: OptionalEmail = None
    email_from_name: Annotated[str, Field(min_length=1, max_length=120)] = (
        "Atcraft Manufacturing ERP"
    )
    smtp_host: str = ""
    smtp_port: Annotated[int, Field(ge=1, le=65_535)] = 587
    smtp_security: SmtpSecurity = SmtpSecurity.starttls
    smtp_username: str = ""
    smtp_password: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    smtp_timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 15

    # ----------------------------------------------------------- business --
    default_currency: str = DEFAULT_CURRENCY
    money_decimal_places: Annotated[int, Field(ge=0, le=6)] = 2
    quantity_decimal_places: Annotated[int, Field(ge=0, le=6)] = 4
    # Above this many demand lines an MRP run is dispatched to Celery rather
    # than executed inline (section 14: never hold HTTP open for minutes).
    mrp_sync_threshold_demand_lines: Annotated[int, Field(ge=1)] = 50
    # Celery beat uses UTC. 20:30 UTC is 02:00 the following day in India.
    # Five-field cron syntax: minute hour day-of-month month day-of-week.
    mrp_schedule_cron: str = "30 20 * * *"
    # A line discount above this percentage needs the
    # ``sales_order.discount_override`` right, and the order then needs
    # approval before it can be confirmed. Without the right the discount is
    # refused outright. Offer-derived discounts are pre-authorised and exempt
    # from the threshold, but still subject to the below-cost check
    # (docs/assumptions.md §E10-3).
    sales_discount_approval_percent: Annotated[Decimal, Field(ge=0, le=100)] = Decimal("10")

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
    def is_deployed(self) -> bool:
        """Whether this is an internet-reachable production-like process."""
        return self.app_env in {AppEnv.staging, AppEnv.production}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allowed_host_list(self) -> list[str]:
        """Trusted hosts plus the names used by internal container probes."""
        hosts = {"localhost", "127.0.0.1", "backend"}
        hosts.update(host.strip().lower().rstrip(".") for host in self.allowed_hosts.split(","))
        for origin in self.cors_origin_list:
            hostname = urlsplit(origin).hostname
            if hostname:
                hosts.add(hostname.lower().rstrip("."))
        return sorted(host for host in hosts if host)

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

    @field_validator("email_from_name", "smtp_host", "smtp_username")
    @classmethod
    def _reject_mail_header_injection(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Mail configuration values cannot contain newlines")
        return value.strip()

    @field_validator("allowed_hosts")
    @classmethod
    def _validate_allowed_hosts(cls, value: str) -> str:
        """Reject values Starlette would parse differently from an operator.

        ``TrustedHostMiddleware`` expects bare DNS names,
        not URLs, host:port pairs or paths.  Silently accepting one of those
        makes the configured public host unreachable and tempts an emergency
        wildcard bypass during a deployment.
        """
        hostname = re.compile(
            r"(?=^.{1,253}\Z)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
            r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z"
        )
        cleaned: list[str] = []
        for raw in value.split(","):
            host = raw.strip().lower().rstrip(".")
            if not host:
                if value:
                    raise ValueError("ALLOWED_HOSTS must not contain an empty entry")
                continue
            if "*" in host or not hostname.fullmatch(host):
                raise ValueError(
                    "ALLOWED_HOSTS entries must be explicit bare DNS names without wildcards"
                )
            cleaned.append(host)
        return ",".join(cleaned)

    @field_validator("mrp_schedule_cron")
    @classmethod
    def _validate_mrp_schedule_cron(cls, value: str) -> str:
        fields = value.split()
        if len(fields) != 5:
            raise ValueError("MRP_SCHEDULE_CRON must use five-field cron syntax")
        return " ".join(fields)

    web_push_enabled: bool = False
    web_push_vapid_private_key: SecretStr = SecretStr("")
    web_push_subject: str = ""
    web_push_ttl_seconds: int = Field(default=86400, ge=0, le=2419200)
    web_push_timeout_seconds: int = Field(default=10, ge=1, le=60)
    web_push_allowed_hosts: str = (
        "fcm.googleapis.com,updates.push.services.mozilla.com,push.services.mozilla.com,"
        "notify.windows.com,web.push.apple.com,push.apple.com"
    )

    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        """Refuse to boot a production process with development-grade secrets.

        A misconfigured production deployment is a security incident, not a
        warning - so this fails loudly at import time rather than serving
        traffic with a guessable signing key.
        """
        if self.is_deployed:
            # Both keys get the same marker-based check. The first version of
            # this guard listed the JWT placeholder from .env.example verbatim
            # and nothing else, so the CREDENTIAL_ENCRYPTION_KEY placeholder
            # shipped in the same template - long enough, and different from
            # the JWT one - booted production with a key anyone holding the
            # repository could read.
            if is_placeholder_secret(self.jwt_secret):
                raise ValueError(
                    "JWT_SECRET must be a strong random value "
                    f"(>={MIN_DEPLOYED_SECRET_LENGTH} chars, not a template placeholder) "
                    "when deployed. Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(64))"'
                )
            encryption_key = self.credential_encryption_key.get_secret_value()
            if is_placeholder_secret(encryption_key):
                raise ValueError(
                    "CREDENTIAL_ENCRYPTION_KEY must be a strong random value "
                    f"(>={MIN_DEPLOYED_SECRET_LENGTH} chars, not a template placeholder) "
                    "when deployed. Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(64))"'
                )
            if secrets.compare_digest(encryption_key, self.jwt_secret):
                raise ValueError(
                    "CREDENTIAL_ENCRYPTION_KEY must be independent from JWT_SECRET so JWT "
                    "rotation does not invalidate encrypted account credentials"
                )
            if self.app_debug:
                raise ValueError("APP_DEBUG must be false when deployed")
            if "*" in self.cors_origin_list and self.cors_allow_credentials:
                raise ValueError("Wildcard CORS origin cannot be combined with credentials")
            if self.email_provider is EmailProvider.disabled:
                raise ValueError(
                    "EMAIL_PROVIDER must be 'smtp' when deployed so password resets can be "
                    "delivered"
                )
            if self.public_web_url.scheme != "https":
                raise ValueError("PUBLIC_WEB_URL must use HTTPS when deployed")
        elif not self.jwt_secret:
            # Development convenience only: an ephemeral per-process key. Tokens
            # do not survive a restart, which is the correct local behaviour.
            object.__setattr__(self, "jwt_secret", secrets.token_urlsafe(64))

        if not self.credential_encryption_key.get_secret_value():
            # Local/test compatibility: one explicit JWT secret still gives
            # every process the same development encryption key. Deployed
            # environments never reach this fallback and must supply an
            # independently generated value above.
            object.__setattr__(
                self,
                "credential_encryption_key",
                SecretStr(self.jwt_secret),
            )

        if self.email_provider is EmailProvider.smtp:
            if not self.smtp_host:
                raise ValueError("SMTP_HOST is required when EMAIL_PROVIDER=smtp")
            if self.email_from_address is None:
                raise ValueError("EMAIL_FROM_ADDRESS is required when EMAIL_PROVIDER=smtp")
            password = self.smtp_password.get_secret_value()
            if bool(self.smtp_username) != bool(password):
                raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together")
            if self.is_deployed and self.smtp_security is SmtpSecurity.none:
                raise ValueError("SMTP must use STARTTLS or TLS when deployed")
        subject = self.web_push_subject or (
            f"mailto:{self.email_from_address}" if self.email_from_address else ""
        )
        if self.web_push_enabled and self.is_deployed and not subject:
            raise ValueError(
                "WEB_PUSH_SUBJECT or EMAIL_FROM_ADDRESS is required when deployed with push enabled"
            )
        if subject and not (subject.startswith("mailto:") or subject.startswith("https://")):
            raise ValueError("WEB_PUSH_SUBJECT must use mailto: or https:")
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

__all__ = [
    "API_ROOT",
    "MIN_DEPLOYED_SECRET_LENGTH",
    "REPO_ROOT",
    "AppEnv",
    "EmailProvider",
    "Settings",
    "SmtpSecurity",
    "get_settings",
    "is_placeholder_secret",
    "settings",
]
