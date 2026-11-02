from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.courier_tracking import provider
from src.modules.courier_tracking.models import CourierStatus
from src.modules.courier_tracking.schemas import GenericWebhookEvent


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Shipment Created", CourierStatus.BOOKED),
        ("picked_up", CourierStatus.PICKED_UP),
        ("In-Transit", CourierStatus.IN_TRANSIT),
        ("Out For Delivery", CourierStatus.OUT_FOR_DELIVERY),
        ("Delivered", CourierStatus.DELIVERED),
        ("Undelivered", CourierStatus.FAILED_DELIVERY),
        ("RTO", CourierStatus.RETURNED),
        ("Lost", CourierStatus.EXCEPTION),
        ("unrecognized provider status", CourierStatus.EXCEPTION),
    ],
)
def test_shiprocket_statuses_normalize_conservatively(raw: str, expected: CourierStatus) -> None:
    assert provider.normalize_status(raw) is expected


def test_generic_signature_requires_valid_hmac_and_fresh_timestamp() -> None:
    body = b'{"events":[]}'
    secret = "webhook-secret"
    timestamp = "1800000000"
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()

    assert provider.verify_generic_signature(
        secret=secret,
        body=body,
        signature=f"sha256={digest}",
        timestamp=timestamp,
        now=1_800_000_100,
    )
    assert not provider.verify_generic_signature(
        secret=secret, body=body, signature="0" * 64, timestamp=timestamp, now=1_800_000_100
    )
    assert not provider.verify_generic_signature(
        secret=secret, body=body, signature=digest, timestamp=timestamp, now=1_800_000_301
    )
    assert not provider.verify_generic_signature(
        secret=secret, body=body, signature=digest, timestamp="not-a-time", now=1_800_000_100
    )


def test_shiprocket_login_payload_is_json() -> None:
    assert json.loads(provider.shiprocket_request_payload("user@example.com", "secret")) == {
        "email": "user@example.com",
        "password": "secret",
    }


def test_shiprocket_parser_maps_activity_and_keeps_provider_id() -> None:
    events = provider.parse_shiprocket_tracking(
        {
            "tracking_data": {
                "shipment_track": [{"awb_code": "AWB-1"}],
                "shipment_track_activities": [
                    {
                        "activity": "Out for Delivery",
                        "date": "2026-11-14T10:30:00+05:30",
                        "location": "Mumbai",
                        "id": "activity-1",
                    }
                ],
            }
        }
    )

    assert events == [
        {
            "tracking_number": "AWB-1",
            "raw_status": "Out for Delivery",
            "event_time": datetime(2026, 11, 14, 5, 0, tzinfo=UTC),
            "location": "Mumbai",
            "description": "Out for Delivery",
            "provider_event_id": "activity-1",
            "raw_payload": {
                "activity": "Out for Delivery",
                "date": "2026-11-14T10:30:00+05:30",
                "location": "Mumbai",
                "id": "activity-1",
            },
        }
    ]


def test_generic_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        GenericWebhookEvent(
            tracking_number="AWB-1",
            status="Delivered",
            event_time=datetime.fromisoformat("2026-11-14T10:30:00"),
        )


def test_shiprocket_request_sends_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200

        @staticmethod
        def read(_: int) -> bytes:
            return b'{"ok":true}'

    class FakeConnection:
        headers: dict[str, str] | None = None

        def __init__(self, host: str, *, timeout: int) -> None:
            assert host == "apiv2.shiprocket.in"
            assert timeout == provider.PROVIDER_TIMEOUT_SECONDS

        def request(
            self, method: str, path: str, *, body: bytes | None, headers: dict[str, str]
        ) -> None:
            assert method == "GET"
            assert path.endswith("AWB-1")
            assert body is None
            FakeConnection.headers = headers

        @staticmethod
        def getresponse() -> FakeResponse:
            return FakeResponse()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(provider.http.client, "HTTPSConnection", FakeConnection)
    assert provider._request_sync("GET", "/track/AWB-1", bearer="access-token") == {"ok": True}
    assert FakeConnection.headers == {
        "Accept": "application/json",
        "Authorization": "Bearer access-token",
    }


def test_invalid_shiprocket_timestamp_is_not_forged_as_current_time() -> None:
    with pytest.raises(provider.CourierProviderError, match="invalid event time"):
        provider._parse_provider_time("not-a-date")
