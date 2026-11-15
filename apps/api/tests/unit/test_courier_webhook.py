"""The courier webhook is decided by the service, before anything is recorded.

``admit_webhook`` runs before the body is read: the deployment switch, then the
``COURIER_WEBHOOK_PER_IP`` flood guard keyed on the sending address. ``receive_webhook``
then authenticates the delivery - the URL's token names the account, a generic
provider signs ``<timestamp>.<body>``, Shiprocket sends the secret as its key -
and parses only an authenticated body. All of it used to live in the route
handler, where nothing short of an HTTP request could reach it.

The token lookup and both ingest paths are replaced by recorders here, and the
session handed through is one that fails on any use: each refusal is shown to
happen before an event could be recorded, and each acceptance to reach exactly
one ingest path. The account is built the way the database hands it back, with
``provider`` a plain string rather than the enum member.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from src.core import ratelimit
from src.core.clock import utcnow
from src.core.config import settings
from src.core.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from src.core.ratelimit import KEY_PREFIX, RateLimitPolicy
from src.core.security import encrypt_courier_webhook_secret, hash_secret_token
from src.modules.courier_tracking import service
from src.modules.courier_tracking.models import CourierAccount, CourierProvider
from src.modules.courier_tracking.provider import (
    GENERIC_SIGNATURE_HEADER,
    SHIPROCKET_KEY_HEADER,
    webhook_header_name,
)
from tests.redis_support import FakeRedis
from tests.route_support import UntouchableSession

pytestmark = pytest.mark.unit

TOKEN = "courier-url-token"
SECRET = "courier-webhook-secret"
GENERIC_BODY = json.dumps(
    {
        "events": [
            {
                "event_id": "event-1",
                "tracking_number": "AWB-1",
                "status": "In Transit",
                "event_time": "2026-11-10T10:00:00+00:00",
            }
        ]
    }
).encode()
SHIPROCKET_BODY = json.dumps({"awb": "AWB-2", "current_status": "Delivered"}).encode()


def _account(provider: str, *, secret: str | None = SECRET) -> CourierAccount:
    """An account as a load returns it: ``provider`` is the column's plain string."""
    return CourierAccount(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        code="COURIER-1",
        provider=provider,
        display_name="Courier",
        webhook_token_hash=hash_secret_token(TOKEN),
        webhook_secret_ciphertext=(
            None if secret is None else encrypt_courier_webhook_secret(secret)
        ),
    )


def _signed(body: bytes, *, secret: str = SECRET, at: int | None = None) -> dict[str, str]:
    timestamp = str(int(time.time()) if at is None else at)
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return {"signature": f"sha256={digest.hexdigest()}", "timestamp": timestamp}


class Delivery:
    """The token lookup and both ingest paths, recording what reached them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.account: CourierAccount | None = None
        self.asked: list[str] = []
        self.generic: list[list[str]] = []
        self.shiprocket: list[dict[str, Any]] = []
        self.touched: list[str] = []
        monkeypatch.setattr(service, "find_account_by_webhook_token", self._find)
        monkeypatch.setattr(service, "ingest_generic_webhook", self._generic)
        monkeypatch.setattr(service, "ingest_shiprocket_webhook", self._shiprocket)

    async def _find(self, session: Any, token_hash: str) -> CourierAccount | None:
        self.asked.append(token_hash)
        return self.account

    async def _generic(self, session: Any, account: CourierAccount, events: Sequence[Any]) -> int:
        self.generic.append([event.tracking_number for event in events])
        return len(events)

    async def _shiprocket(
        self, session: Any, account: CourierAccount, payload: dict[str, Any]
    ) -> int:
        self.shiprocket.append(payload)
        return 1

    async def receive(self, body: bytes, **headers: str) -> int:
        session: Any = UntouchableSession(self.touched)
        return await service.receive_webhook(session, TOKEN, body, **headers)

    @property
    def recorded(self) -> bool:
        return bool(self.generic or self.shiprocket)


@pytest.fixture
def delivery(monkeypatch: pytest.MonkeyPatch) -> Delivery:
    return Delivery(monkeypatch)


# -----------------------------------------------------------------------------
# Which account, and whether it may deliver at all
# -----------------------------------------------------------------------------


async def test_the_account_is_looked_up_by_the_tokens_digest_never_the_token(
    delivery: Delivery,
) -> None:
    delivery.account = _account(CourierProvider.SHIPROCKET.value)

    assert await delivery.receive(SHIPROCKET_BODY, api_key=SECRET) == 1

    assert delivery.asked == [hash_secret_token(TOKEN)]
    assert TOKEN not in delivery.asked
    assert delivery.touched == []


@pytest.mark.parametrize("issued", [False, True], ids=["unknown token", "no secret issued"])
async def test_an_unknown_token_and_an_unissued_secret_are_the_same_404(
    delivery: Delivery, issued: bool
) -> None:
    delivery.account = _account("GENERIC_WEBHOOK", secret=None) if issued else None

    with pytest.raises(NotFoundError) as refused:
        await delivery.receive(GENERIC_BODY, **_signed(GENERIC_BODY))

    assert refused.value.message == NotFoundError("Courier webhook").message
    assert not delivery.recorded


# -----------------------------------------------------------------------------
# A generic provider: a fresh HMAC over <timestamp>.<body>
# -----------------------------------------------------------------------------


async def test_a_signed_generic_delivery_reaches_the_generic_ingest(delivery: Delivery) -> None:
    delivery.account = _account("GENERIC_WEBHOOK")

    assert await delivery.receive(GENERIC_BODY, **_signed(GENERIC_BODY)) == 1

    assert delivery.generic == [["AWB-1"]]
    assert delivery.shiprocket == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no signature"),
        pytest.param(_signed(GENERIC_BODY, secret="another-secret"), id="wrong secret"),
        pytest.param(_signed(b'{"events":[]}'), id="signs another body"),
        pytest.param(_signed(GENERIC_BODY, at=int(time.time()) - 3600), id="stale timestamp"),
        pytest.param(
            {**_signed(GENERIC_BODY), "timestamp": "not-a-time"}, id="unreadable timestamp"
        ),
        pytest.param({"api_key": SECRET}, id="a key instead of a signature"),
    ],
)
async def test_an_unauthenticated_generic_delivery_is_a_401_before_it_is_parsed(
    delivery: Delivery, headers: dict[str, str]
) -> None:
    delivery.account = _account("GENERIC_WEBHOOK")

    with pytest.raises(AuthenticationError) as refused:
        await delivery.receive(GENERIC_BODY, **headers)

    assert refused.value.message == "Courier webhook signature is invalid."
    assert not delivery.recorded


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json", id="not json"),
        pytest.param(b'{"events": []}', id="no events"),
        pytest.param(b'{"events": [{"tracking_number": "AWB-1"}]}', id="event missing fields"),
        pytest.param(
            json.dumps(
                {
                    "events": [
                        {
                            "tracking_number": "AWB-1",
                            "status": "In Transit",
                            "event_time": "2026-11-10T10:00:00",
                        }
                    ]
                }
            ).encode(),
            id="naive event time",
        ),
    ],
)
async def test_a_signed_but_malformed_generic_body_is_a_422(
    delivery: Delivery, body: bytes
) -> None:
    delivery.account = _account("GENERIC_WEBHOOK")

    with pytest.raises(ValidationError) as refused:
        await delivery.receive(body, **_signed(body))

    assert refused.value.message == "Courier webhook payload is invalid."
    assert not delivery.recorded


# -----------------------------------------------------------------------------
# Shiprocket: the secret itself, as the webhook key
# -----------------------------------------------------------------------------


async def test_a_keyed_shiprocket_delivery_reaches_the_shiprocket_ingest(
    delivery: Delivery,
) -> None:
    delivery.account = _account("SHIPROCKET")

    assert await delivery.receive(SHIPROCKET_BODY, api_key=SECRET) == 1

    assert delivery.shiprocket == [{"awb": "AWB-2", "current_status": "Delivered"}]
    assert delivery.generic == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no key"),
        pytest.param({"api_key": ""}, id="empty key"),
        pytest.param({"api_key": "another-secret"}, id="wrong key"),
        pytest.param(_signed(SHIPROCKET_BODY), id="a signature instead of a key"),
    ],
)
async def test_an_unkeyed_shiprocket_delivery_is_a_401_before_it_is_parsed(
    delivery: Delivery, headers: dict[str, str]
) -> None:
    delivery.account = _account("SHIPROCKET")

    with pytest.raises(AuthenticationError) as refused:
        await delivery.receive(SHIPROCKET_BODY, **headers)

    assert refused.value.message == "Courier webhook token is invalid."
    assert not delivery.recorded


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(b"not json", "Courier webhook payload is invalid.", id="not json"),
        pytest.param(b"\xff\xfe\x00", "Courier webhook payload is invalid.", id="not utf-8"),
        pytest.param(b"[1, 2]", "Courier webhook payload must be an object.", id="an array"),
        pytest.param(b'"AWB-2"', "Courier webhook payload must be an object.", id="a string"),
    ],
)
async def test_a_keyed_but_malformed_shiprocket_body_is_a_422(
    delivery: Delivery, body: bytes, message: str
) -> None:
    delivery.account = _account("SHIPROCKET")

    with pytest.raises(ValidationError) as refused:
        await delivery.receive(body, api_key=SECRET)

    assert refused.value.message == message
    assert not delivery.recorded


# -----------------------------------------------------------------------------
# Before the body is read: the switch, then the flood guard
# -----------------------------------------------------------------------------


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    stub = FakeRedis()

    async def _get_redis() -> FakeRedis:
        return stub

    monkeypatch.setattr(ratelimit, "get_redis", _get_redis)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    return stub


def _exhaust(redis: FakeRedis, address: str) -> None:
    scope, limit, _ = RateLimitPolicy.COURIER_WEBHOOK_PER_IP
    now_ms = int(utcnow().timestamp() * 1000)
    redis.windows[f"{KEY_PREFIX}:rl:{scope}:{address}"] = [
        (now_ms, f"spent-{n}") for n in range(limit)
    ]


def test_the_flood_guard_is_sixty_deliveries_a_minute() -> None:
    """docs/security.md D3 names it; a silent widening is visible here."""
    assert RateLimitPolicy.COURIER_WEBHOOK_PER_IP == ("courier_webhook", 60, 60)


async def test_a_disabled_deployment_refuses_before_spending_the_budget(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
) -> None:
    monkeypatch.setattr(settings, "courier_tracking_enabled", False)

    with pytest.raises(ValidationError, match="Courier tracking is disabled"):
        await service.admit_webhook("203.0.113.7")

    assert redis.windows == {}


async def test_the_flood_guard_is_spent_per_sending_address(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
) -> None:
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    scope = RateLimitPolicy.COURIER_WEBHOOK_PER_IP[0]

    await service.admit_webhook("203.0.113.7")
    await service.admit_webhook(None)

    assert set(redis.windows) == {
        f"{KEY_PREFIX}:rl:{scope}:203.0.113.7",
        f"{KEY_PREFIX}:rl:{scope}:unknown",
    }


async def test_an_exhausted_address_is_a_429_and_another_address_is_admitted(
    monkeypatch: pytest.MonkeyPatch, redis: FakeRedis
) -> None:
    monkeypatch.setattr(settings, "courier_tracking_enabled", True)
    _exhaust(redis, "203.0.113.7")

    with pytest.raises(RateLimitedError) as refused:
        await service.admit_webhook("203.0.113.7")

    assert refused.value.retry_after_seconds >= 1
    await service.admit_webhook("198.51.100.4")


# -----------------------------------------------------------------------------
# Which header the operator is told to configure
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "header"),
    [
        ("SHIPROCKET", SHIPROCKET_KEY_HEADER),
        (CourierProvider.SHIPROCKET, SHIPROCKET_KEY_HEADER),
        ("GENERIC_WEBHOOK", GENERIC_SIGNATURE_HEADER),
        (CourierProvider.GENERIC_WEBHOOK, GENERIC_SIGNATURE_HEADER),
    ],
)
def test_the_header_name_follows_the_provider_however_it_was_loaded(
    provider: str, header: str
) -> None:
    """A loaded account's provider is a plain string; ``is`` against the enum
    member told every Shiprocket operator to configure the signature header."""
    assert webhook_header_name(provider) == header
