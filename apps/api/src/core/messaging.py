"""Allow-listed HTTPS transports for WhatsApp Cloud API and DLT SMS gateways."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from src.core.config import SmsProvider, settings


class MessagingOutcome(StrEnum):
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    RETRY = "RETRY"


@dataclass(frozen=True, slots=True)
class MessagingResult:
    outcome: MessagingOutcome
    provider_message_id: str | None = None
    error_code: str | None = None


def outcome_for_status(status: int) -> MessagingOutcome:
    if 200 <= status < 300:
        return MessagingOutcome.DELIVERED
    if status == 429 or status >= 500:
        return MessagingOutcome.RETRY
    return MessagingOutcome.FAILED


def assert_allowed_url(url: str) -> None:
    """Reject non-HTTPS, non-default ports, IP literals and unlisted hosts."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid messaging provider URL.") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 for char in url)
    ):
        raise ValueError("Messaging provider URL must use HTTPS on the default port.")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Messaging providers cannot use IP addresses.")
    allowed = {
        host.strip().lower().rstrip(".")
        for host in settings.messaging_allowed_hosts.split(",")
        if host.strip()
    }
    if hostname.lower().rstrip(".") not in allowed:
        raise ValueError("Messaging provider host is not allow-listed.")


def _request(url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> MessagingResult:
    """Perform one bounded HTTPS POST without following redirects or logging content."""
    assert_allowed_url(url)
    parsed = urlsplit(url)
    connection = http.client.HTTPSConnection(
        parsed.hostname or "", port=parsed.port or 443, timeout=settings.messaging_timeout_seconds
    )
    try:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        connection.request("POST", parsed.path or "/", body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(16_384)
        outcome = outcome_for_status(response.status)
        provider_id = None
        if outcome is MessagingOutcome.DELIVERED and response_body:
            try:
                data = json.loads(response_body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                data = None
            provider_id = _provider_message_id(data)
        error_code = None if outcome is MessagingOutcome.DELIVERED else f"http_{response.status}"
        return MessagingResult(outcome, provider_message_id=provider_id, error_code=error_code)
    except (OSError, http.client.HTTPException):
        return MessagingResult(MessagingOutcome.RETRY, error_code="transport_error")
    finally:
        connection.close()


def _provider_message_id(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("message_id", "request_id", "messageId"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value[:255]
    messages = data.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        value = messages[0].get("id")
        if isinstance(value, str) and value:
            return value[:255]
    return None


class WhatsAppCloudProvider:
    """WhatsApp Cloud API template sender."""

    async def send_template(
        self,
        to_e164: str,
        template_name: str,
        language: str,
        parameters: Sequence[str],
    ) -> MessagingResult:
        if not settings.whatsapp_enabled:
            return MessagingResult(MessagingOutcome.FAILED, error_code="channel_disabled")
        if not re.fullmatch(
            r"v[0-9]+(?:\.[0-9]+){0,2}", settings.whatsapp_api_version
        ) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", settings.whatsapp_phone_number_id):
            return MessagingResult(MessagingOutcome.FAILED, error_code="invalid_provider_config")
        url = (
            f"{settings.whatsapp_api_base.rstrip('/')}/"
            f"{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
        )
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language},
        }
        if parameters:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": value} for value in parameters],
                }
            ]
        payload = {
            "messaging_product": "whatsapp",
            "to": to_e164,
            "type": "template",
            "template": template,
        }
        return await asyncio.to_thread(
            _request,
            url,
            payload=payload,
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )


class Msg91SmsProvider:
    """MSG91 Flow API sender for an approved TRAI DLT template."""

    async def send_template(
        self,
        to_e164: str,
        template_id: str,
        parameters: Sequence[str],
    ) -> MessagingResult:
        variables = {f"VAR{index}": value for index, value in enumerate(parameters, start=1)}
        payload: dict[str, Any] = {
            "template_id": template_id,
            "sender": settings.sms_sender_id,
            "short_url": "0",
            "mobiles": to_e164.removeprefix("+"),
            **variables,
        }
        headers = {
            "authkey": settings.sms_auth_key.get_secret_value(),
            "DLT_TE_ID": settings.sms_dlt_entity_id,
            "Content-Type": "application/json",
        }
        return await asyncio.to_thread(
            _request, settings.sms_api_url, payload=payload, headers=headers
        )


class GenericJsonSmsProvider:
    """Configurable JSON gateway using the agreed DLT template/variables contract."""

    async def send_template(
        self,
        to_e164: str,
        template_id: str,
        parameters: Sequence[str],
    ) -> MessagingResult:
        payload = {
            "template_id": template_id,
            "sender": settings.sms_sender_id,
            "entity_id": settings.sms_dlt_entity_id,
            "to": to_e164,
            "variables": list(parameters),
        }
        return await asyncio.to_thread(
            _request,
            settings.sms_api_url,
            payload=payload,
            headers={
                "Authorization": f"Bearer {settings.sms_auth_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )


def sms_provider() -> Msg91SmsProvider | GenericJsonSmsProvider:
    if settings.sms_provider is SmsProvider.msg91:
        return Msg91SmsProvider()
    return GenericJsonSmsProvider()


__all__ = [
    "GenericJsonSmsProvider",
    "MessagingOutcome",
    "MessagingResult",
    "Msg91SmsProvider",
    "WhatsAppCloudProvider",
    "assert_allowed_url",
    "outcome_for_status",
    "sms_provider",
]
