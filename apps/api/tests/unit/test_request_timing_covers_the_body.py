"""A streamed response is timed to its last byte, not its first.

The access log and the ``atcraft_http_request_duration`` histogram both used to
time a request either side of ``BaseHTTPMiddleware.call_next``, which returns
when the ASGI ``http.response.start`` message arrives - that is, when the
*headers* are ready. Both were therefore recording time-to-first-byte under the
name "duration".

For JSON the two are microseconds apart. For ``GET /documents/{id}/download``,
which streams, the recorded duration was the row lookup and the object open with
the whole transfer excluded - so a 40 MB drawing over a site VPN logged as a few
milliseconds, ``request.slow`` could never fire for a download, and the histogram
reported the slowest route in the ERP as the fastest.

These tests drive the real middleware over a real streaming response with a real
delay in the middle, and assert the recorded duration contains it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import src.core.middleware as middleware
from src.core.middleware import RequestContextMiddleware, is_final_body_message

pytestmark = pytest.mark.unit

#: Long enough to dwarf any scheduling noise, short enough not to slow the suite.
BODY_DELAY_SECONDS = 0.2
BODY_DELAY_MS = BODY_DELAY_SECONDS * 1000


def _app() -> Starlette:
    async def slow_body(_request: Any) -> Response:
        async def chunks() -> AsyncIterator[bytes]:
            yield b"first"
            # The transfer, standing in for a large download over a slow link.
            await asyncio.sleep(BODY_DELAY_SECONDS)
            yield b"last"

        return StreamingResponse(chunks(), media_type="text/plain")

    async def plain(_request: Any) -> Response:
        return PlainTextResponse("ok")

    async def boom(_request: Any) -> Response:
        raise RuntimeError("no")

    app = Starlette(
        routes=[Route("/stream", slow_body), Route("/plain", plain), Route("/boom", boom)]
    )
    app.add_middleware(RequestContextMiddleware)
    return app


def test_the_access_log_measures_the_whole_download(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        middleware.log, "info", lambda event, **kw: captured.append({"event": event, **kw})
    )

    with TestClient(_app()) as client:
        assert client.get("/stream").text == "firstlast"

    completed = [c for c in captured if c["event"] == "request.completed"]
    assert len(completed) == 1, "exactly one access log line per request"
    entry = completed[0]

    assert entry["duration_ms"] >= BODY_DELAY_MS, (
        "duration excludes the body - it is timing to first byte again "
        f"({entry['duration_ms']} ms for a {BODY_DELAY_MS} ms body)"
    )
    assert entry["first_byte_ms"] < BODY_DELAY_MS, (
        "first_byte_ms should be the headers alone, before the delay"
    )
    assert entry["status"] == 200


def test_a_slow_download_is_flagged_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    """The warning that exists to find slow endpoints must reach the slow one."""
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(middleware, "SLOW_REQUEST_MS", BODY_DELAY_MS / 2)
    monkeypatch.setattr(middleware.log, "info", lambda *a, **kw: None)
    monkeypatch.setattr(
        middleware.log, "warning", lambda event, **kw: warnings.append({"event": event, **kw})
    )

    with TestClient(_app()) as client:
        client.get("/stream")

    assert [w["event"] for w in warnings] == ["request.slow"]


def test_the_response_time_header_is_still_first_byte() -> None:
    """It is sent ahead of the body, so it cannot honestly be anything else."""
    with TestClient(_app()) as client:
        response = client.get("/stream")

    assert float(response.headers["X-Response-Time-ms"]) < BODY_DELAY_MS
    assert response.headers["X-Request-ID"]


def test_an_ordinary_response_logs_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(middleware.log, "info", lambda event, **kw: captured.append(event))

    with TestClient(_app()) as client:
        assert client.get("/plain").text == "ok"

    assert captured == ["request.completed"]


def test_a_failing_request_is_logged_once_and_reraised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exception path must not also emit a completion line."""
    completed: list[str] = []
    failed: list[str] = []
    monkeypatch.setattr(middleware.log, "info", lambda event, **kw: completed.append(event))
    monkeypatch.setattr(middleware.log, "exception", lambda event, **kw: failed.append(event))

    with TestClient(_app(), raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500

    assert failed == ["request.failed"]
    assert completed == []


def test_the_correlation_id_is_honoured_when_the_caller_supplies_one() -> None:
    with TestClient(_app()) as client:
        response = client.get("/plain", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"


def test_the_final_body_message_is_the_one_without_more_body() -> None:
    assert is_final_body_message({"type": "http.response.body", "body": b"x"}) is True
    assert (
        is_final_body_message({"type": "http.response.body", "body": b"x", "more_body": True})
        is False
    )
    assert is_final_body_message({"type": "http.response.start", "status": 200}) is False
