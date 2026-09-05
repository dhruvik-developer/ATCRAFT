"""Browser refresh-session cookies and CSRF binding.

The refresh JWT is HttpOnly and never appears in JSON. A second, readable
cookie contains an HMAC of that exact JWT. JavaScript echoes it in a header;
the server compares cookie, header and HMAC before rotating the credential.
An attacker who can cause a cross-site form submission cannot read or forge the
header, and cookie tossing from a sibling host cannot forge the HMAC.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from fastapi import Request, Response

from src.core.config import AppEnv, settings
from src.core.errors import CsrfValidationError

REFRESH_COOKIE_NAME = "atcraft_refresh"
CSRF_COOKIE_NAME = "atcraft_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"


def _secure_cookie() -> bool:
    return settings.app_env in {AppEnv.staging, AppEnv.production}


def _refresh_cookie_path() -> str:
    return f"{settings.api_v1_prefix.rstrip('/')}/auth"


def csrf_token_for(refresh_token: str) -> str:
    digest = hmac.new(
        settings.jwt_secret.encode(),
        f"atcraft:refresh-csrf:v1:{refresh_token}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def set_refresh_cookies(response: Response, refresh_token: str) -> None:
    max_age = settings.jwt_refresh_token_expire_days * 86_400
    secure = _secure_cookie()
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=max_age,
        path=_refresh_cookie_path(),
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token_for(refresh_token),
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def clear_refresh_cookies(response: Response) -> None:
    secure = _secure_cookie()
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=_refresh_cookie_path(),
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def refresh_token_from(request: Request) -> str | None:
    return request.cookies.get(REFRESH_COOKIE_NAME)


def require_refresh_csrf(request: Request, refresh_token: str) -> None:
    cookie_value = request.cookies.get(CSRF_COOKIE_NAME, "")
    header_value = request.headers.get(CSRF_HEADER_NAME, "")
    expected = csrf_token_for(refresh_token)
    if not (
        cookie_value
        and header_value
        and hmac.compare_digest(cookie_value, header_value)
        and hmac.compare_digest(cookie_value, expected)
    ):
        raise CsrfValidationError()


__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "REFRESH_COOKIE_NAME",
    "clear_refresh_cookies",
    "csrf_token_for",
    "refresh_token_from",
    "require_refresh_csrf",
    "set_refresh_cookies",
]
