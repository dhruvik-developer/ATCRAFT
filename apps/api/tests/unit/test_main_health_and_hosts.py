"""Process-edge invariants for host validation and dependency probes."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import src.main as main_module
from src.core.config import AppEnv
from src.core.middleware import TrustedHostMiddleware

pytestmark = pytest.mark.unit


def test_trusted_host_is_the_outermost_user_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "app_env", AppEnv.staging)

    application = main_module.create_app()

    assert application.user_middleware[0].cls is TrustedHostMiddleware


async def test_invalid_host_is_rejected_before_request_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module.settings, "app_env", AppEnv.staging)
    monkeypatch.setattr(main_module.settings, "allowed_hosts", "erp.example.test")
    application = main_module.create_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://attacker.example.test",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 400
    assert response.json() == {
        "success": False,
        "error": {"code": "VALIDATION_ERROR", "message": "Invalid host header", "details": None},
        "request_id": None,
    }
    assert "x-request-id" not in response.headers
    assert "x-content-type-options" not in response.headers


async def test_readiness_checks_dependencies_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    both_started = asyncio.Event()
    started = 0

    async def waits_for_peer() -> bool:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        return True

    monkeypatch.setattr(main_module, "check_database_ready", waits_for_peer)
    monkeypatch.setattr(main_module, "check_redis_ready", waits_for_peer)
    monkeypatch.setattr(main_module, "DEPENDENCY_CHECK_TIMEOUT_SECONDS", 0.1)

    result = await asyncio.wait_for(main_module._dependency_readiness(), timeout=0.5)

    assert result == (True, True)


async def test_readiness_fails_closed_when_a_dependency_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = asyncio.Event()

    async def never_finishes() -> bool:
        try:
            await asyncio.Event().wait()
            return True
        finally:
            cancelled.set()

    async def available() -> bool:
        return True

    monkeypatch.setattr(main_module.settings, "app_env", AppEnv.test)
    monkeypatch.setattr(main_module, "check_database_ready", never_finishes)
    monkeypatch.setattr(main_module, "check_redis_ready", available)
    monkeypatch.setattr(main_module, "DEPENDENCY_CHECK_TIMEOUT_SECONDS", 0.01)
    application = main_module.create_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Resolve FastAPI's lazy route tree before timing dependency handling;
        # coverage and concurrent browser work can make that unrelated setup slow.
        await client.get("/health/live")
        response = await asyncio.wait_for(client.get("/health/ready"), timeout=0.5)

    assert response.status_code == 503
    assert cancelled.is_set()
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": False, "redis": True},
    }


async def test_cross_origin_preflight_admits_the_csrf_header_and_exposes_rate_limit_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORS only matters when the SPA is on another origin, which is exactly
    where the missing X-CSRF-Token allow-list entry would have refused every
    mutating request at the preflight."""
    monkeypatch.setattr(main_module.settings, "app_env", AppEnv.test)
    monkeypatch.setattr(main_module.settings, "cors_origins", "https://app.example.test")
    application = main_module.create_app()
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.options(
            "/api/v1/auth/refresh",
            headers={
                "Origin": "https://app.example.test",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, x-csrf-token, authorization",
            },
        )

    assert response.status_code == 200, response.text
    allowed = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"x-csrf-token", "content-type", "authorization"} <= allowed
    assert "X-CSRF-Token" in main_module.CORS_ALLOWED_REQUEST_HEADERS
    assert {"Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining"} <= set(
        main_module.CORS_EXPOSED_RESPONSE_HEADERS
    )
