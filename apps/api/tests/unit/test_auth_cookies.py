"""Refresh-cookie confidentiality and CSRF binding at the HTTP boundary."""

from __future__ import annotations

import pytest
from fastapi import Request, Response

from src.core.errors import CsrfValidationError, TokenExpiredError
from src.core.exception_handlers import app_error_handler
from src.modules.auth.cookies import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    REFRESH_COOKIE_NAME,
    csrf_token_for,
    require_refresh_csrf,
    set_refresh_cookies,
)

pytestmark = pytest.mark.unit

REFRESH = "signed-refresh-credential"


def _request(*, csrf_cookie: str = "", csrf_header: str = "") -> Request:
    cookies = f"{REFRESH_COOKIE_NAME}={REFRESH}"
    if csrf_cookie:
        cookies += f"; {CSRF_COOKIE_NAME}={csrf_cookie}"
    headers = [(b"cookie", cookies.encode())]
    if csrf_header:
        headers.append((CSRF_HEADER_NAME.lower().encode(), csrf_header.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/refresh",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("127.0.0.1", 1),
        }
    )


def test_refresh_credential_is_http_only_and_scoped_to_auth() -> None:
    response = Response()
    set_refresh_cookies(response, REFRESH)
    headers = response.headers.getlist("set-cookie")
    refresh_header = next(value for value in headers if value.startswith(REFRESH_COOKIE_NAME))
    csrf_header = next(value for value in headers if value.startswith(CSRF_COOKIE_NAME))

    assert "HttpOnly" in refresh_header
    assert "SameSite=strict" in refresh_header
    assert "Path=/api/v1/auth" in refresh_header
    assert "HttpOnly" not in csrf_header
    assert REFRESH not in csrf_header
    assert "Path=/" in csrf_header


def test_csrf_value_must_match_cookie_header_and_refresh_hmac() -> None:
    csrf = csrf_token_for(REFRESH)
    require_refresh_csrf(_request(csrf_cookie=csrf, csrf_header=csrf), REFRESH)

    with pytest.raises(CsrfValidationError):
        require_refresh_csrf(_request(csrf_cookie=csrf), REFRESH)
    with pytest.raises(CsrfValidationError):
        require_refresh_csrf(_request(csrf_cookie=csrf, csrf_header="attacker"), REFRESH)
    with pytest.raises(CsrfValidationError):
        require_refresh_csrf(_request(csrf_cookie="attacker", csrf_header="attacker"), REFRESH)


async def test_rejected_refresh_expires_both_stale_browser_cookies() -> None:
    response = await app_error_handler(_request(), TokenExpiredError())

    headers = response.headers.getlist("set-cookie")
    assert response.status_code == 401
    assert any(
        value.startswith(f"{REFRESH_COOKIE_NAME}=") and "Max-Age=0" in value for value in headers
    )
    assert any(
        value.startswith(f"{CSRF_COOKIE_NAME}=") and "Max-Age=0" in value for value in headers
    )


async def test_csrf_failure_does_not_destroy_a_valid_refresh_session() -> None:
    response = await app_error_handler(_request(), CsrfValidationError())

    assert response.status_code == 403
    assert response.headers.getlist("set-cookie") == []
