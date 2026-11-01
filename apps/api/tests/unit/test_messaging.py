"""Provider request shape and HTTPS allow-list checks without network access."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from src.core.config import SmsProvider, settings
from src.core.messaging import (
    GenericJsonSmsProvider,
    MessagingOutcome,
    MessagingResult,
    Msg91SmsProvider,
    WhatsAppCloudProvider,
    assert_allowed_url,
    outcome_for_status,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, MessagingOutcome.DELIVERED),
        (299, MessagingOutcome.DELIVERED),
        (400, MessagingOutcome.FAILED),
        (429, MessagingOutcome.RETRY),
        (503, MessagingOutcome.RETRY),
    ],
)
def test_http_status_maps_to_provider_outcome(status: int, expected: MessagingOutcome) -> None:
    assert outcome_for_status(status) is expected


def test_provider_url_requires_https_and_an_allow_listed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    assert_allowed_url("https://control.msg91.com/api/v5/flow/")
    for url in (
        "http://control.msg91.com/api/v5/flow/",
        "https://127.0.0.1/api/v5/flow/",
        "https://evil.example/api/v5/flow/",
        "https://control.msg91.com:8443/api/v5/flow/",
    ):
        with pytest.raises(ValueError):
            assert_allowed_url(url)


def test_whatsapp_provider_sends_the_approved_template_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.messaging as messaging

    monkeypatch.setattr(settings, "whatsapp_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_api_base", "https://graph.facebook.com")
    monkeypatch.setattr(settings, "whatsapp_api_version", "v20.0")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")
    monkeypatch.setattr(settings, "whatsapp_access_token", SecretStr("private-token"))
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "graph.facebook.com")
    captured: dict[str, Any] = {}

    def request(url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> MessagingResult:
        captured.update(url=url, payload=payload, headers=headers)
        return MessagingResult(MessagingOutcome.DELIVERED)

    monkeypatch.setattr(messaging, "_request", request)
    result = asyncio.run(
        WhatsAppCloudProvider().send_template(
            "+919876543210", "purchase_update", "en", ["PO-1", "Ready"]
        )
    )
    assert result.outcome is MessagingOutcome.DELIVERED
    assert captured == {
        "url": "https://graph.facebook.com/v20.0/123/messages",
        "payload": {
            "messaging_product": "whatsapp",
            "to": "+919876543210",
            "type": "template",
            "template": {
                "name": "purchase_update",
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": "PO-1"},
                            {"type": "text", "text": "Ready"},
                        ],
                    }
                ],
            },
        },
        "headers": {
            "Authorization": "Bearer private-token",
            "Content-Type": "application/json",
        },
    }


def test_msg91_sends_registered_template_and_ordered_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.messaging as messaging

    monkeypatch.setattr(settings, "sms_auth_key", SecretStr("private-key"))
    monkeypatch.setattr(settings, "sms_sender_id", "ATCRFT")
    monkeypatch.setattr(settings, "sms_dlt_entity_id", "entity-1")
    monkeypatch.setattr(settings, "sms_api_url", "https://control.msg91.com/api/v5/flow/")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "control.msg91.com")
    captured: dict[str, Any] = {}

    def request(url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> MessagingResult:
        captured.update(url=url, payload=payload, headers=headers)
        return MessagingResult(MessagingOutcome.DELIVERED)

    monkeypatch.setattr(messaging, "_request", request)
    result = asyncio.run(
        Msg91SmsProvider().send_template("+919876543210", "dlt-123", ["PO-1", "Ready"])
    )
    assert result.outcome is MessagingOutcome.DELIVERED
    assert captured == {
        "url": "https://control.msg91.com/api/v5/flow/",
        "payload": {
            "template_id": "dlt-123",
            "sender": "ATCRFT",
            "short_url": "0",
            "mobiles": "919876543210",
            "VAR1": "PO-1",
            "VAR2": "Ready",
        },
        "headers": {
            "authkey": "private-key",
            "DLT_TE_ID": "entity-1",
            "Content-Type": "application/json",
        },
    }


def test_generic_sms_uses_the_configured_template_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.messaging as messaging

    monkeypatch.setattr(settings, "sms_sender_id", "ATCRFT")
    monkeypatch.setattr(settings, "sms_dlt_entity_id", "entity-1")
    monkeypatch.setattr(settings, "sms_api_url", "https://sms.example.test/send")
    monkeypatch.setattr(settings, "messaging_allowed_hosts", "sms.example.test")
    monkeypatch.setattr(settings, "sms_provider", SmsProvider.generic_json)
    captured: dict[str, Any] = {}

    def request(url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> MessagingResult:
        captured.update(url=url, payload=payload, headers=headers)
        return MessagingResult(MessagingOutcome.DELIVERED)

    monkeypatch.setattr(messaging, "_request", request)
    result = asyncio.run(
        GenericJsonSmsProvider().send_template("+919876543210", "dlt-123", ["PO-1"])
    )
    assert result.outcome is MessagingOutcome.DELIVERED
    assert captured["payload"] == {
        "template_id": "dlt-123",
        "sender": "ATCRFT",
        "entity_id": "entity-1",
        "to": "+919876543210",
        "variables": ["PO-1"],
    }
