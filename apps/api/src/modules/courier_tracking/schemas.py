"""Courier integration request and response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from src.modules.courier_tracking.models import CourierProvider, CourierStatus

Code = Annotated[str, Field(min_length=1, max_length=32, pattern=r"^\S(?:.*\S)?$")]
DisplayName = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^\S(?:.*\S)?$")]


class ShiprocketCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: SecretStr


class CourierAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Code
    provider: CourierProvider
    display_name: DisplayName
    credentials: ShiprocketCredentials | None = None
    transporter_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    auto_confirm_delivery: bool = True
    is_active: bool = True

    @model_validator(mode="after")
    def credentials_match_provider(self) -> CourierAccountCreate:
        if self.provider is CourierProvider.SHIPROCKET and self.credentials is None:
            raise ValueError("Shiprocket credentials are required.")
        if self.provider is CourierProvider.GENERIC_WEBHOOK and self.credentials is not None:
            raise ValueError("Generic webhook accounts do not use API credentials.")
        return self


class CourierAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName | None = None
    credentials: ShiprocketCredentials | None = None
    transporter_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    auto_confirm_delivery: bool | None = None
    is_active: bool | None = None


class CourierAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    provider: CourierProvider
    display_name: str
    is_active: bool
    auto_confirm_delivery: bool
    has_credentials: bool
    has_webhook: bool
    last_poll_at: datetime | None
    last_error: str | None


class RotateWebhookRead(BaseModel):
    webhook_url: str
    secret: str
    header_name: str


class CourierTestRead(BaseModel):
    connected: bool
    provider: CourierProvider


class CourierRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dispatch_id: uuid.UUID | None = None
    shipment_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> CourierRefreshRequest:
        if (self.dispatch_id is None) == (self.shipment_id is None):
            raise ValueError("Provide exactly one of dispatch_id or shipment_id.")
        return self


class CourierEventFilters(BaseModel):
    tracking_number: Annotated[str, Field(max_length=120)] | None = None
    dispatch_id: uuid.UUID | None = None
    shipment_id: uuid.UUID | None = None


class CourierEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    courier_account_id: uuid.UUID
    tracking_number: str
    normalized_status: CourierStatus
    raw_status: str
    event_time: datetime
    location: str | None
    description: str | None
    provider_event_id: str | None
    dispatch_id: uuid.UUID | None
    shipment_id: uuid.UUID | None
    created_at: datetime


class GenericWebhookEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    tracking_number: Annotated[str, Field(min_length=1, max_length=120)]
    status: Annotated[str, Field(min_length=1, max_length=120)]
    event_time: datetime
    location: Annotated[str, Field(max_length=255)] | None = None
    description: Annotated[str, Field(max_length=500)] | None = None

    @field_validator("event_time")
    @classmethod
    def event_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Courier event time must include a timezone.")
        return value


class GenericWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[GenericWebhookEvent] = Field(min_length=1, max_length=100)
