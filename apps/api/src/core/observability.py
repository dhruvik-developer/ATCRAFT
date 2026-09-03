"""Metrics and tracing (master rules section 56).

Prometheus counters/histograms are always available; OpenTelemetry is opt-in via
``OTEL_ENABLED`` so a development machine does not need a collector running.

Label discipline matters more than metric count: labels are cartesian, so a
metric labelled with a raw path or a record id produces an unbounded number of
series and will eventually take Prometheus down. Everything here labels on the
route *template*.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
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

# --- Database -----------------------------------------------------------------

db_query_duration = Histogram(
    "atcraft_db_query_duration_seconds",
    "Database query latency by logical operation",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
    registry=registry,
)

db_pool_in_use = Gauge(
    "atcraft_db_pool_connections_in_use",
    "Connections currently checked out of the pool",
    registry=registry,
)

# --- Cache --------------------------------------------------------------------

cache_operations = Counter(
    "atcraft_cache_operations_total",
    "Cache operations by namespace and result",
    ["namespace", "result"],
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

celery_queue_depth = Gauge(
    "atcraft_celery_queue_depth",
    "Messages waiting in a Celery queue",
    ["queue"],
    registry=registry,
)

# --- Business signals ---------------------------------------------------------
# Section 56 lists infrastructure metrics; these are the ones that actually get
# somebody out of bed, because they mean the ERP is producing wrong answers.

inventory_conflicts = Counter(
    "atcraft_inventory_lock_conflicts_total",
    "Reservation attempts that lost a row lock and were retried or rejected",
    ["operation"],
    registry=registry,
)

insufficient_stock_events = Counter(
    "atcraft_insufficient_stock_total",
    "Reservation or allocation attempts refused for lack of available stock",
    registry=registry,
)

negative_balance_blocked = Counter(
    "atcraft_negative_balance_blocked_total",
    "Writes rejected by the non-negative stock constraint - always investigate",
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

approval_events = Counter(
    "atcraft_approval_events_total",
    "Approval decisions by entity type and outcome",
    ["entity", "decision"],
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


def setup_sentry() -> None:
    """Application error tracking, if a DSN is configured."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=str(settings.app_env),
            traces_sample_rate=0.1,
            # Section 54: never ship request bodies or headers to a third party;
            # they contain business data and bearer tokens.
            send_default_pii=False,
            max_request_body_size="never",
        )
        log.info("sentry.enabled", environment=str(settings.app_env))
    except ImportError:
        log.warning("sentry.unavailable", hint='pip install ".[sentry]"')


__all__ = [
    "approval_events",
    "cache_operations",
    "celery_queue_depth",
    "celery_task_duration",
    "celery_tasks_total",
    "db_pool_in_use",
    "db_query_duration",
    "http_request_duration",
    "http_requests_total",
    "insufficient_stock_events",
    "inventory_conflicts",
    "mrp_run_duration",
    "mrp_runs_total",
    "negative_balance_blocked",
    "registry",
    "render_metrics",
    "setup_sentry",
    "setup_tracing",
]
