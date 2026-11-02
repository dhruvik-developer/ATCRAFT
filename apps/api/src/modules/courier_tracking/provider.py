"""Shiprocket adapter and pure webhook/status helpers."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import anyio

from src.core.config import settings
from src.modules.courier_tracking.models import (
    WEBHOOK_CLOCK_SKEW_SECONDS,
    CourierStatus,
)

SHIPROCKET_API = "https://apiv2.shiprocket.in"
SHIPROCKET_LOGIN_PATH = "/v1/external/auth/login"
SHIPROCKET_TRACK_PATH = "/v1/external/courier/track/awb/"
PROVIDER_TIMEOUT_SECONDS = 10
PROVIDER_RESPONSE_BYTES = 64 * 1024

_STATUS_WORDS: dict[str, CourierStatus] = {
    "BOOKED": CourierStatus.BOOKED,
    "NEW": CourierStatus.BOOKED,
    "MANIFESTED": CourierStatus.BOOKED,
    "SHIPMENT CREATED": CourierStatus.BOOKED,
    "PICKED UP": CourierStatus.PICKED_UP,
    "SHIPMENT PICKED UP": CourierStatus.PICKED_UP,
    "PICKUP": CourierStatus.PICKED_UP,
    "IN TRANSIT": CourierStatus.IN_TRANSIT,
    "INTRANSIT": CourierStatus.IN_TRANSIT,
    "SHIPMENT IN TRANSIT": CourierStatus.IN_TRANSIT,
    "REACHED AT DESTINATION HUB": CourierStatus.IN_TRANSIT,
    "ARRIVED AT DESTINATION HUB": CourierStatus.IN_TRANSIT,
    "SHIPPED": CourierStatus.IN_TRANSIT,
    "OUT FOR DELIVERY": CourierStatus.OUT_FOR_DELIVERY,
    "OUTFORDELIVERY": CourierStatus.OUT_FOR_DELIVERY,
    "SHIPMENT OUT FOR DELIVERY": CourierStatus.OUT_FOR_DELIVERY,
    "DELIVERED": CourierStatus.DELIVERED,
    "DELIVERED TO CONSIGNEE": CourierStatus.DELIVERED,
    "RTO": CourierStatus.RETURNED,
    "RTO INITIATED": CourierStatus.RETURNED,
    "RTO DELIVERED": CourierStatus.RETURNED,
    "RETURNED": CourierStatus.RETURNED,
    "RETURN TO ORIGIN": CourierStatus.RETURNED,
    "UNDELIVERED": CourierStatus.FAILED_DELIVERY,
    "DELIVERY ATTEMPT FAILED": CourierStatus.FAILED_DELIVERY,
    "DELIVERY NOT ATTEMPTED": CourierStatus.FAILED_DELIVERY,
    "DELIVERY FAILED": CourierStatus.FAILED_DELIVERY,
    "FAILED DELIVERY": CourierStatus.FAILED_DELIVERY,
    "EXCEPTION": CourierStatus.EXCEPTION,
    "LOST": CourierStatus.EXCEPTION,
    "DAMAGED": CourierStatus.EXCEPTION,
}


class CourierProviderError(RuntimeError):
    """Safe provider failure without response bodies or credentials."""


def normalize_status(raw_status: str) -> CourierStatus:
    key = re.sub(r"[_-]+", " ", " ".join(raw_status.upper().split()))
    # Unknown provider labels must never imply delivery or another lifecycle
    # transition. They surface for staff review as an exception.
    return _STATUS_WORDS.get(key, CourierStatus.EXCEPTION)


def verify_generic_signature(
    *, secret: str, body: bytes, signature: str, timestamp: str, now: float | None = None
) -> bool:
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return False
    moment = time.time() if now is None else now
    if abs(moment - stamp) > WEBHOOK_CLOCK_SKEW_SECONDS:
        return False
    supplied = signature.removeprefix("sha256=").lower()
    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied, expected)


def shiprocket_request_payload(email: str, password: str) -> bytes:
    return json.dumps({"email": email, "password": password}, separators=(",", ":")).encode()


def _allowed_shiprocket_host() -> str:
    allowed = {
        host.strip().lower().rstrip(".")
        for host in settings.courier_allowed_hosts.split(",")
        if host.strip()
    }
    parsed = urlsplit(SHIPROCKET_API)
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise CourierProviderError("Shiprocket host is not allow-listed.")
    return parsed.hostname


def _request_sync(
    method: str, path: str, *, payload: bytes | None = None, bearer: str | None = None
) -> dict[str, Any]:
    host = _allowed_shiprocket_host()
    connection = http.client.HTTPSConnection(host, timeout=PROVIDER_TIMEOUT_SECONDS)
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if bearer is not None:
        headers["Authorization"] = "Bearer " + bearer
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        body = response.read(PROVIDER_RESPONSE_BYTES + 1)
        if len(body) > PROVIDER_RESPONSE_BYTES:
            raise CourierProviderError("Courier provider response exceeded the size limit.")
        if response.status < 200 or response.status >= 300:
            raise CourierProviderError(f"Courier provider returned HTTP {response.status}.")
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise CourierProviderError("Courier provider returned an invalid response.")
        return decoded
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        raise CourierProviderError("Courier provider request failed.") from exc
    finally:
        connection.close()


async def shiprocket_login(email: str, password: str) -> str:
    payload = shiprocket_request_payload(email, password)
    response = await anyio.to_thread.run_sync(
        lambda: _request_sync("POST", SHIPROCKET_LOGIN_PATH, payload=payload)
    )
    token = response.get("token")
    if not isinstance(token, str) or not token:
        raise CourierProviderError("Courier provider did not return an access token.")
    return token


async def shiprocket_track(awb: str, token: str) -> dict[str, Any]:
    response = await anyio.to_thread.run_sync(
        lambda: _request_sync("GET", SHIPROCKET_TRACK_PATH + quote(awb, safe=""), bearer=token)
    )
    return response


def parse_shiprocket_tracking(
    payload: dict[str, Any], *, fallback_tracking_number: str | None = None
) -> list[dict[str, Any]]:
    data = payload.get("tracking_data")
    if isinstance(data, dict):
        tracks = data.get("shipment_track")
        if isinstance(tracks, list) and tracks:
            shipment = tracks[0]
            activities = data.get("shipment_track_activities", [])
            if isinstance(activities, list) and activities:
                events: list[dict[str, Any]] = []
                for activity in activities[:100]:
                    if not isinstance(activity, dict):
                        continue
                    status = (
                        activity.get("activity")
                        or activity.get("status")
                        or data.get("shipment_status")
                    )
                    moment = activity.get("date") or data.get("track_created_at")
                    events.append(
                        {
                            "tracking_number": str(
                                shipment.get("awb_code") or fallback_tracking_number or ""
                            ),
                            "raw_status": str(status or "unknown")[:120],
                            "event_time": _parse_provider_time(moment),
                            "location": _optional_text(activity.get("location"), 255),
                            "description": _optional_text(activity.get("activity"), 500),
                            "provider_event_id": _optional_text(activity.get("id"), 160),
                            "raw_payload": activity,
                        }
                    )
                if events:
                    return events
            return [
                {
                    "tracking_number": str(
                        shipment.get("awb_code") or fallback_tracking_number or ""
                    ),
                    "raw_status": str(
                        data.get("current_status")
                        or data.get("shipment_status")
                        or shipment.get("current_status")
                        or "unknown"
                    )[:120],
                    "event_time": _parse_provider_time(data.get("track_created_at")),
                    "location": _optional_text(shipment.get("destination"), 255),
                    "description": _optional_text(data.get("current_status"), 500),
                    "provider_event_id": None,
                    "raw_payload": payload,
                }
            ]
    raise CourierProviderError("Courier provider returned no tracking data.")


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


def _parse_provider_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CourierProviderError("Courier provider returned an invalid event time.") from exc
    else:
        # ASSUMPTION (?D3-n): when Shiprocket omits an event time, timestamp it
        # at observation time; malformed timestamps are rejected.
        moment = datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


__all__ = [
    "CourierProviderError",
    "normalize_status",
    "parse_shiprocket_tracking",
    "shiprocket_login",
    "shiprocket_request_payload",
    "shiprocket_track",
    "verify_generic_signature",
]
