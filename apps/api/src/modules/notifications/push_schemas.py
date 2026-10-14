"""Public device shapes deliberately exclude provider credentials."""

from __future__ import annotations

import uuid
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.webpush import assert_allowed_endpoint, decode_b64url


class PushConfigRead(BaseModel):
    enabled: bool
    vapid_public_key: str | None


class PushKeys(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p256dh: str = Field(min_length=1, max_length=128)
    auth: str = Field(min_length=1, max_length=32)

    @field_validator("p256dh")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        raw = decode_b64url(value)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError("A push key must be an uncompressed P-256 point.")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        return value

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, value: str) -> str:
        if len(decode_b64url(value)) != 16:
            raise ValueError("Push authentication must contain 16 bytes.")
        return value


class PushEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=4096)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        assert_allowed_endpoint(value)
        return value


class PushSubscribeRequest(PushEndpointRequest):
    keys: PushKeys
    expiration_time: int | None = Field(default=None, ge=0)
    device_label: str | None = Field(default=None, max_length=120)


class PushSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    device_label: str | None
    user_agent: str | None
    created_at: datetime
    last_success_at: datetime | None
    failure_count: int


class PushDeleteRead(BaseModel):
    removed: bool


class PushTestRead(BaseModel):
    queued: int
