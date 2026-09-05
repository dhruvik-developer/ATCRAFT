"""Who the coarse per-client API budget is charged to.

The middleware runs before authentication. Its first version keyed on a digest
of whatever the ``Authorization`` header said, so ``Bearer <anything random>``
minted a fresh bucket per request and the budget was never consumed - only
callers who cooperated were limited. The key is now the *verified* subject of
an access token, and anything that does not verify is charged to the client
address like every other anonymous request.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from starlette.requests import Request

from src.core.middleware import _bearer_rate_limit_identity, _rate_limit_identity
from src.core.security import create_access_token, create_refresh_token

pytestmark = pytest.mark.unit


def _request(*, authorization: str | None = None, real_ip: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if real_ip is not None:
        headers.append((b"x-real-ip", real_ip.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/products/items",
            "headers": headers,
            "query_string": b"",
            "scheme": "https",
            "server": ("test", 443),
            "client": ("10.0.0.7", 1),
        }
    )


def test_a_verified_access_token_is_charged_to_its_subject() -> None:
    subject, company = uuid.uuid4(), uuid.uuid4()
    token, _ = create_access_token(subject, company, session_id="s1")

    identity = _bearer_rate_limit_identity(f"Bearer {token}")

    assert identity == f"user:{subject}"
    assert token not in identity


def test_the_identity_is_stable_across_tokens_for_one_user() -> None:
    """Two tabs, two sessions, one budget - and the same on every worker."""
    subject, company = uuid.uuid4(), uuid.uuid4()
    first, _ = create_access_token(subject, company, session_id="s1")
    second, _ = create_access_token(subject, company, session_id="s2")

    assert _bearer_rate_limit_identity(f"Bearer {first}") == _bearer_rate_limit_identity(
        f"Bearer {second}"
    )


@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer not-a-token",
        "Bearer " + "a" * 200,
        "Bearer",
        "Basic dXNlcjpwYXNz",
        "",
    ],
)
def test_an_unverifiable_header_gets_no_identity_of_its_own(authorization: str) -> None:
    assert _bearer_rate_limit_identity(authorization) is None


def test_a_refresh_token_presented_as_a_bearer_credential_is_not_trusted() -> None:
    subject, company = uuid.uuid4(), uuid.uuid4()
    refresh, _ = create_refresh_token(subject, company, session_id="s1")

    assert _bearer_rate_limit_identity(f"Bearer {refresh}") is None


def test_an_expired_token_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.core import security

    subject, company = uuid.uuid4(), uuid.uuid4()
    real_utcnow = security.utcnow
    monkeypatch.setattr(security, "utcnow", lambda: real_utcnow() - timedelta(days=2))
    token, _ = create_access_token(subject, company, session_id="s1")
    monkeypatch.setattr(security, "utcnow", real_utcnow)

    assert _bearer_rate_limit_identity(f"Bearer {token}") is None


def test_a_header_rotating_client_shares_one_address_bucket() -> None:
    """The actual attack: every request with a different junk bearer."""
    identities = {
        _rate_limit_identity(_request(authorization=f"Bearer junk-{n}", real_ip="203.0.113.9"))
        for n in range(5)
    }
    assert identities == {"ip:203.0.113.9"}


def test_an_anonymous_request_is_charged_to_the_proxied_address() -> None:
    assert _rate_limit_identity(_request(real_ip="203.0.113.9")) == "ip:203.0.113.9"
    assert _rate_limit_identity(_request()) == "ip:10.0.0.7"


def test_a_verified_request_is_charged_to_the_user_not_the_address() -> None:
    subject, company = uuid.uuid4(), uuid.uuid4()
    token, _ = create_access_token(subject, company, session_id="s1")

    identity = _rate_limit_identity(_request(authorization=f"Bearer {token}", real_ip="1.2.3.4"))

    assert identity == f"user:{subject}"
