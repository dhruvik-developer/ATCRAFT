"""Metrics and tracing (master rules section 56).

Prometheus counters/histograms are always available; OpenTelemetry is opt-in via
``OTEL_ENABLED`` so a development machine does not need a collector running.

Label discipline matters more than metric count: labels are cartesian, so a
metric labelled with a raw path or a record id produces an unbounded number of
series and will eventually take Prometheus down. Everything here labels on the
route *template*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)

registry = CollectorRegistry()

# --- HTTP ---------------------------------------------------------------------

http_requests_total = Counter(
    "atcraft_http_requests_total",
    "HTTP requests processed",
    ["method", "route", "status"],
    registry=registry,
)

http_request_duration = Histogram(
    "atcraft_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    # Buckets straddle the section 57 targets (100 ms cached, 200 ms normal) so
    # the SLO can be read straight off the histogram.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=registry,
)

# --- Background jobs ----------------------------------------------------------

celery_tasks_total = Counter(
    "atcraft_celery_tasks_total",
    "Celery tasks by name and outcome",
    ["task", "status"],
    registry=registry,
)

celery_task_duration = Histogram(
    "atcraft_celery_task_duration_seconds",
    "Celery task runtime",
    ["task"],
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 300, 900),
    registry=registry,
)

# --- Business signals ---------------------------------------------------------
# Section 56 lists infrastructure metrics; these are the ones that actually get
# somebody out of bed, because they mean the ERP is producing wrong answers.
#
# Every metric declared here is observed somewhere: prometheus_client exports a
# registered series immediately, so a declared-but-never-incremented counter
# advertises a permanent 0 and an operator alerting on it gets a false
# all-clear. Add a metric together with the ``.inc()`` / ``.observe()`` at its
# seam, or not at all.

inventory_conflicts = Counter(
    "atcraft_inventory_lock_conflicts_total",
    "Stock postings refused because a row lock was not granted in time",
    ["operation"],
    registry=registry,
)

insufficient_stock_events = Counter(
    "atcraft_insufficient_stock_total",
    "Stock postings and reservations refused for lack of available stock",
    registry=registry,
)

mrp_runs_total = Counter(
    "atcraft_mrp_runs_total",
    "MRP runs by execution mode and outcome",
    ["mode", "status"],
    registry=registry,
)

mrp_run_duration = Histogram(
    "atcraft_mrp_run_duration_seconds",
    "End-to-end MRP run time",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
    registry=registry,
)


def render_metrics() -> tuple[bytes, str]:
    """Payload for ``GET /metrics``."""
    return generate_latest(registry), CONTENT_TYPE_LATEST


def setup_tracing(app: Any) -> None:
    """Install OpenTelemetry instrumentation when enabled and available.

    Deliberately tolerant: tracing is diagnostics. A missing exporter must never
    prevent the ERP from starting.
    """
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.otel_service_name,
                    "deployment.environment": str(settings.app_env),
                }
            )
        )
        if settings.otel_exporter_otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
            )
        trace.set_tracer_provider(provider)

        from src.core.database import engine

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/.*,metrics")
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        RedisInstrumentor().instrument()
        log.info("otel.enabled", endpoint=settings.otel_exporter_otlp_endpoint or "console")
    except ImportError:
        log.warning("otel.unavailable", hint='install the optional extra: pip install ".[otel]"')
    except Exception as exc:
        log.warning("otel.setup_failed", error=str(exc))


if TYPE_CHECKING:  # pragma: no cover - typing only
    from sentry_sdk.types import Event, Hint


def _scrub_sentry_event(event: Event, _hint: Hint) -> Event | None:
    """Remove request-adjacent data before an exception leaves the deployment.

    ``send_default_pii=False`` is necessary but does not mean "send no request
    context". Headers, URLs, breadcrumbs and arbitrary logging extras can still
    contain account numbers, document identifiers or search terms. Stack frames
    and the exception itself are enough to diagnose a server fault, so keep
    those and discard the data-bearing context unconditionally.

    Typed with Sentry's own ``Event``/``Hint`` rather than ``dict[str, Any]``:
    the SDK declares ``before_send`` as returning ``Event | None`` (``None``
    drops the event), and while sentry-sdk was merely an optional extra mypy
    saw ``init`` as untyped and could not check this signature at all. It is a
    ``TypedDict``, so ``pop`` needs the cast below - the keys are dynamic by
    design here.
    """
    scrubbed = cast(dict[str, Any], event)
    for key in ("request", "user", "breadcrumbs", "extra"):
        scrubbed.pop(key, None)
    return event


def setup_sentry() -> None:
    """Application error tracking, if a DSN is configured."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=str(settings.app_env),
            # Prometheus owns performance telemetry. Sentry receives exceptions
            # only: transaction spans can include SQL and request URLs.
            traces_sample_rate=0.0,
            # Section 54: never ship request bodies or headers to a third party;
            # they contain business data and bearer tokens.
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            max_breadcrumbs=0,
            before_send=_scrub_sentry_event,
        )
        log.info("sentry.enabled", environment=str(settings.app_env))
    except ImportError:
        # sentry-sdk is a runtime dependency, not an extra: it is installed with
        # the application and SENTRY_DSN alone switches it on. Reaching here
        # means the environment was not installed from the lock.
        log.warning(
            "sentry.unavailable",
            hint=(
                "sentry-sdk is installed with the application and SENTRY_DSN enables it; "
                "reinstall from the lock to restore it"
            ),
        )


__all__ = [
    "celery_task_duration",
    "celery_tasks_total",
    "http_request_duration",
    "http_requests_total",
    "insufficient_stock_events",
    "inventory_conflicts",
    "mrp_run_duration",
    "mrp_runs_total",
    "registry",
    "render_metrics",
    "setup_sentry",
    "setup_tracing",
]
