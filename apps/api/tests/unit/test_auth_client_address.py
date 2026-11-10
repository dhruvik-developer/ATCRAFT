"""The sign-in routes resolve the caller's address the way the middleware does.

``auth.router.client_ip`` used to parse ``X-Real-IP`` by itself: it took the
first entry of a comma list, accepted a zoned IPv6 address that PostgreSQL's
``INET`` refuses, and answered None for an unparsable header instead of falling
back to the socket peer - so one request could be logged under one address and
charged to the sign-in budgets under another, or under the shared ``unknown``
bucket. It now delegates to :func:`src.core.logging.resolve_client_ip`, and
these tests hold the two paths together.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from src.core.logging import resolve_client_ip
from src.core.middleware import _rate_limit_identity
from src.modules.auth.router import client_ip

pytestmark = pytest.mark.unit

PEER = "192.0.2.1"


def _request(*, real_ip: str | None, peer: str | None) -> Request:
    headers = [] if real_ip is None else [(b"x-real-ip", real_ip.encode())]
    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "headers": headers,
        "query_string": b"",
        "scheme": "https",
        "server": ("test", 443),
        "client": None if peer is None else (peer, 1234),
    }
    return Request(scope)


@pytest.mark.parametrize(
    ("real_ip", "peer", "expected"),
    [
        pytest.param("203.0.113.9", PEER, "203.0.113.9", id="proxy-header-wins"),
        pytest.param("2001:0db8::1", PEER, "2001:db8::1", id="ipv6-normalised"),
        pytest.param(None, PEER, PEER, id="no-header-uses-peer"),
        pytest.param("fe80::1%eth0", PEER, PEER, id="zoned-ipv6-uses-peer"),
        pytest.param("203.0.113.9, 10.0.0.1", PEER, PEER, id="comma-list-uses-peer"),
        pytest.param("not-an-address", PEER, PEER, id="garbage-uses-peer"),
        pytest.param("not-an-address", "testclient", None, id="nothing-usable"),
        pytest.param(None, None, None, id="no-header-no-peer"),
    ],
)
def test_sign_in_routes_resolve_the_address_the_middleware_resolves(
    real_ip: str | None, peer: str | None, expected: str | None
) -> None:
    request = _request(real_ip=real_ip, peer=peer)

    resolved = client_ip(request)

    assert resolved == expected
    assert resolved == resolve_client_ip(real_ip, peer)
    assert _rate_limit_identity(request) == f"ip:{resolved or 'unknown'}"


@pytest.mark.parametrize("header", ["fe80::1%eth0", "fe80::1%25eth0", "::1%lo"])
def test_a_zoned_address_never_reaches_an_inet_column(header: str) -> None:
    """``ipaddress`` parses a scope id; PostgreSQL's ``INET`` cannot store one."""
    assert client_ip(_request(real_ip=header, peer=None)) is None
    assert client_ip(_request(real_ip=header, peer=PEER)) == PEER
