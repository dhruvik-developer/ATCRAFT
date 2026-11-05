"""Broker-backed Celery queue and worker status probes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from celery.exceptions import CeleryError
from kombu.exceptions import OperationalError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.config import settings
from src.workers.celery_app import celery_app

REDIS_CONNECT_TIMEOUT_SECONDS = 0.5
REDIS_COMMAND_TIMEOUT_SECONDS = 0.5
WORKER_PING_TIMEOUT_SECONDS = 1.0

_broker: Redis | None = None


class QueueMonitoringUnavailableError(RuntimeError):
    """The broker-backed queue or worker probe could not be completed."""


@dataclass(frozen=True)
class QueueSnapshot:
    queue_depths: dict[str, int]
    workers_online: int


def queue_names() -> tuple[str, ...]:
    """Return queues declared by the Celery default and task routes."""
    queues = {str(celery_app.conf.task_default_queue)}
    routes = celery_app.conf.task_routes
    if isinstance(routes, Mapping):
        for route in routes.values():
            if isinstance(route, Mapping):
                queue = route.get("queue")
                if isinstance(queue, str):
                    queues.add(queue)
    return tuple(sorted(queues))


async def _get_broker() -> Redis:
    global _broker
    if _broker is None:
        if not settings.celery_broker_url.startswith(("redis://", "rediss://")):
            raise QueueMonitoringUnavailableError("Queue monitoring requires a Redis Celery broker")
        try:
            _broker = Redis.from_url(
                settings.celery_broker_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=8,
                socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
                socket_timeout=REDIS_COMMAND_TIMEOUT_SECONDS,
                retry_on_timeout=False,
            )
        except ValueError as exc:
            raise QueueMonitoringUnavailableError("Celery broker URL is invalid") from exc
    return _broker


def _discard_broker() -> None:
    global _broker
    _broker = None


def _ping_workers() -> Mapping[str, Any] | None:
    inspector = celery_app.control.inspect(timeout=WORKER_PING_TIMEOUT_SECONDS)
    return inspector.ping()


async def get_queue_snapshot() -> QueueSnapshot:
    """Read ready queue lengths and count workers responding to a broker ping."""
    names = queue_names()
    broker = await _get_broker()
    try:
        # redis-py's shared stubs include sync return types for the async client.
        lengths = (cast(Awaitable[int], broker.llen(name)) for name in names)
        depths = await asyncio.gather(*lengths)
    except asyncio.CancelledError:
        _discard_broker()
        raise
    except (OSError, RedisError, TimeoutError) as exc:
        _discard_broker()
        raise QueueMonitoringUnavailableError("Celery broker queue probe failed") from exc

    try:
        workers = await asyncio.to_thread(_ping_workers)
    except (CeleryError, OSError, OperationalError, TimeoutError) as exc:
        raise QueueMonitoringUnavailableError("Celery worker probe failed") from exc

    return QueueSnapshot(
        queue_depths=dict(zip(names, depths, strict=True)),
        workers_online=len(workers or {}),
    )


async def close_queue_monitoring() -> None:
    """Close the broker pool during application shutdown."""
    global _broker
    if _broker is not None:
        await _broker.aclose()
        _broker = None
